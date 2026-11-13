"""The content worker: one process per fetch, hard timeout, tree kill."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server_fetch.server as server_module
from mcp_server_fetch import _worker
from mcp_server_fetch.server import (
    _WORKER_FAILED,
    _WORKERS_BUSY,
    _fetch_response,
    _new_slots,
    _run_worker,
    _worker_launch,
)

from .harness import FakeWeb, connect
from .test_protocol import ARTICLE_HTML, PAGE

ARTICLE = ARTICLE_HTML
SRC = Path(server_module.__file__).parent


def req(html: str = ARTICLE, node: Any = None, **kw: Any) -> dict[str, Any]:
    return {
        "mode": "markdown",
        "html": html,
        "page_url": PAGE,
        "links": False,
        "sanitize": True,
        "node": node,
        "budget": 30.0,
        "node_timeout": 20.0,
        **kw,
    }


def node_argv(script: Path, *args: str) -> list[str]:
    return [sys.executable, "-I", str(script), *args]


@pytest.fixture
def fake_nodes(tmp_path: Path) -> dict[str, Path]:
    scripts = {
        "sleeper": "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "sys.stdin.read()\ntime.sleep(60)\n",
        "echo": "import sys, json\nsys.stdout.write(json.dumps(sys.stdin.read()))\n",
        "bad": "raise SystemExit(3)\n",
        "text": "print('not json')\n",
        "num": "print(42)\n",
    }
    for name, body in scripts.items():
        (tmp_path / f"{name}.py").write_text(body)
    return {name: tmp_path / f"{name}.py" for name in scripts}


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        tasks = ["tasklist", "/FI", f"PID eq {pid}", "/NH"]
        return (
            str(pid)
            in subprocess.run(tasks, capture_output=True, check=False, text=True).stdout
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone(pid: int, within: float = 5.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.1)
    return not _alive(pid)


@pytest.fixture
def procs(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []
    real = anyio.open_process

    async def spy(*a: Any, **k: Any) -> Any:
        p = await real(*a, **k)
        seen.append(p)
        return p

    monkeypatch.setattr(anyio, "open_process", spy)
    return seen


async def test_fetch_url_returns_the_body_undecided(web: FakeWeb) -> None:
    response = httpx.Response(
        200, content=ARTICLE.encode(), headers={"content-type": "text/html"}
    )
    web.add(PAGE, response)
    assert await _fetch_response(PAGE, "ua", allow_private_ips=True) == (
        ARTICLE,
        "text/html",
        PAGE,
    )


async def test_worker_round_trip_in_a_real_process() -> None:
    out = await _run_worker(req(), _new_slots(), None)
    assert out == _worker.run(req())
    assert set(out) == {"content", "links", "report"}


def test_worker_imports_no_mcp_stack() -> None:
    script = (
        "import sys, mcp_server_fetch._worker\n"
        "print(sorted(m for m in ('mcp', 'httpx', 'anyio', 'pydantic') if m in sys.modules))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert done.stdout.strip() == "[]", done.stderr


def _main_with(
    run_body: str, request: dict[str, Any]
) -> subprocess.CompletedProcess[bytes]:
    """Run `_worker.main()` in a real interpreter with `run` replaced by `run_body`."""
    script = f"import mcp_server_fetch._worker as w\n{run_body}\nw.main()\n"
    data = json.dumps(request).encode()
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=data,
        capture_output=True,
        check=False,
    )


def test_worker_stdout_is_isolated() -> None:
    body = (
        "real = w.run\ndef run(r):\n    print('noise')\n    return real(r)\nw.run = run"
    )
    done = _main_with(body, req())
    assert done.returncode == 0, done.stderr
    assert set(json.loads(done.stdout)) == {"content", "links", "report"}
    assert b"noise" in done.stderr  # went to fd 2, not the result channel


async def test_worker_limits_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def check_native_memory_limit() -> None:
        refused = tmp_path / "allocation-refused"
        script = (
            "from pathlib import Path\n"
            f"try: x = bytearray({_worker._MEMORY_LIMIT + 64 * 1024 * 1024})\n"
            f"except MemoryError: Path({str(refused)!r}).write_text('refused')\n"
        )
        result = await _run_worker(
            req(node=[sys.executable, "-I", "-c", script]), _new_slots(), None
        )
        assert refused.read_text() == "refused"
        assert result["content"] == _worker.run(req())["content"]

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        query = ctypes.WinDLL("kernel32", use_last_error=True).QueryInformationJobObject
        query.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        contain = server_module._contain
        seen: list[int] = []

        def inspect_job(pid: int) -> int | None:
            job = contain(pid)
            assert job is not None, "worker was not assigned to its Job Object"
            limits = server_module._JobLimits()
            assert query(job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None), (
                ctypes.get_last_error()
            )
            assert limits.LimitFlags & 0x2000  # kill every child when job closes
            assert limits.LimitFlags & 0x100  # per-process memory ceiling
            assert limits.ProcessMemoryLimit == server_module._WORKER_MEMORY
            seen.append(pid)
            return job

        monkeypatch.setattr(server_module, "_contain", inspect_job)
        await check_native_memory_limit()
        assert seen
        return

    body = (
        "import resource\n"
        "def run(r):\n"
        "    limits = {n: resource.getrlimit(getattr(resource, n))[0]\n"
        "              for n in ('RLIMIT_CPU', 'RLIMIT_CORE', 'RLIMIT_DATA')}\n"
        "    return {'content': repr(limits), 'links': '', 'report': {}}\n"
        "w.run = run"
    )
    done = _main_with(body, req())
    assert done.returncode == 0, done.stderr
    limits = ast.literal_eval(json.loads(done.stdout)["content"])
    assert limits["RLIMIT_CPU"] <= 35 and limits["RLIMIT_CORE"] == 0
    if sys.platform == "linux":  # Linux enforces the data-segment limit.
        assert limits["RLIMIT_DATA"] <= _worker._MEMORY_LIMIT
        await check_native_memory_limit()


async def test_worker_timeout_kills_the_process_tree(
    monkeypatch: pytest.MonkeyPatch,
    fake_nodes: dict[str, Path],
    tmp_path: Path,
    procs: list[Any],
) -> None:
    monkeypatch.setattr(server_module, "_WORKER_TIMEOUT", 2)
    pidfile = tmp_path / "pid"
    t0 = time.monotonic()
    request = req(node=node_argv(fake_nodes["sleeper"], str(pidfile)), node_timeout=60)
    with pytest.raises(ToolError, match="took longer than 2 s"):
        await _run_worker(request, _new_slots(), None)
    assert time.monotonic() - t0 < 7
    grandchild = int(pidfile.read_text())
    assert await _gone(procs[0].pid)
    assert await _gone(grandchild)


async def test_cancellation_kills_the_process_tree(
    fake_nodes: dict[str, Path], tmp_path: Path, procs: list[Any]
) -> None:
    pidfile = tmp_path / "pid"
    request = req(node=node_argv(fake_nodes["sleeper"], str(pidfile)), node_timeout=60)
    grandchild: int | None = None
    cancelled_at: float | None = None
    async with anyio.create_task_group() as group:
        group.start_soon(_run_worker, request, _new_slots(), None)
        with anyio.fail_after(15):
            while not pidfile.exists() or not pidfile.read_text():
                await anyio.sleep(0.01)
        grandchild = int(pidfile.read_text())
        cancelled_at = time.monotonic()
        group.cancel_scope.cancel()
    assert cancelled_at is not None and grandchild is not None
    assert time.monotonic() - cancelled_at < 7
    assert await _gone(procs[0].pid)
    assert await _gone(grandchild)


async def test_waiting_calls_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """4 run, 8 wait, the 13th is refused at once; nothing is encoded while waiting."""
    started = asyncio.Event()
    release = asyncio.Event()
    encoded: list[int] = []

    async def spawn(data: bytes) -> dict[str, Any]:
        encoded.append(len(data))
        started.set()
        await release.wait()
        return {"content": "x", "links": "", "report": {}}

    monkeypatch.setattr(server_module, "_spawn_worker", spawn)
    slots = _new_slots()
    calls = [asyncio.create_task(_run_worker(req(), slots, None)) for _ in range(12)]
    await started.wait()
    await asyncio.sleep(0.05)
    assert len(encoded) == 4  # the 8 waiters hold no encoded request
    with pytest.raises(ToolError) as refused:
        await _run_worker(req(), slots, None)
    assert str(refused.value) == _WORKERS_BUSY
    release.set()
    assert len(await asyncio.gather(*calls)) == 12


async def test_text_pipeline_shares_the_admission_cap() -> None:
    slots = _new_slots()
    for _ in range(12):
        await slots.admitted.acquire()
    with pytest.raises(ToolError) as refused:
        await server_module._render(
            "plain",
            "text/plain",
            page_url=PAGE,
            raw=False,
            sanitize=True,
            include_links=False,
            node=None,
            slots=slots,
            deadline=None,
        )
    assert str(refused.value) == _WORKERS_BUSY


async def test_text_pipeline_wait_ends_at_the_deadline() -> None:
    slots = _new_slots()
    for _ in range(4):
        await slots.running.acquire()
    deadline = asyncio.get_running_loop().time() + 0.1
    with pytest.raises(ToolError) as late:
        await server_module._render(
            "plain",
            "text/plain",
            page_url=PAGE,
            raw=False,
            sanitize=True,
            include_links=False,
            node=None,
            slots=slots,
            deadline=deadline,
        )
    assert str(late.value) == _WORKERS_BUSY


async def test_slot_wait_ends_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()

    async def spawn(data: bytes) -> dict[str, Any]:
        await release.wait()
        return {"content": "x", "links": "", "report": {}}

    monkeypatch.setattr(server_module, "_spawn_worker", spawn)
    slots = _new_slots()
    busy = [asyncio.create_task(_run_worker(req(), slots, None)) for _ in range(4)]
    await asyncio.sleep(0.05)
    deadline = asyncio.get_running_loop().time() + 0.2
    with pytest.raises(ToolError) as late:
        await _run_worker(req(), slots, deadline)
    assert str(late.value) == _WORKERS_BUSY
    release.set()
    await asyncio.gather(*busy)


def test_node_timeout_falls_back_to_pure_python(
    fake_nodes: dict[str, Path], tmp_path: Path
) -> None:
    t0 = time.monotonic()
    out = _worker.run(
        req(
            node=node_argv(fake_nodes["sleeper"], str(tmp_path / "pid")), node_timeout=1
        )
    )
    assert time.monotonic() - t0 < 10
    assert out["content"] == _worker.run(req())["content"]


@pytest.mark.parametrize("script", ["bad", "text", "num"])
def test_node_failure_falls_back(fake_nodes: dict[str, Path], script: str) -> None:
    out = _worker.run(req(node=node_argv(fake_nodes[script])))
    assert out["content"] == _worker.run(req())["content"]


def test_node_output_is_used(fake_nodes: dict[str, Path]) -> None:
    out = _worker.run(req(html="<h1>Echoed</h1>", node=node_argv(fake_nodes["echo"])))
    assert out["content"].strip() == "# Echoed"


def test_node_skipped_without_budget(
    fake_nodes: dict[str, Path], tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    out = _worker.run(
        req(node=node_argv(fake_nodes["sleeper"], str(pidfile)), budget=5.5)
    )
    assert not pidfile.exists()
    assert out["content"] == _worker.run(req())["content"]


async def test_worker_crash_is_a_tool_error() -> None:
    with pytest.raises(ToolError) as e:
        await _run_worker(req(mode="bogus"), _new_slots(), None)
    assert str(e.value) == _WORKER_FAILED


async def test_oversized_result_is_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "_MAX_WORKER_OUTPUT", 10)
    with pytest.raises(ToolError) as e:
        await _run_worker(req(), _new_slots(), None)
    assert str(e.value) == _WORKER_FAILED


async def test_deep_nesting_hits_the_worker_timeout(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "_WORKER_TIMEOUT", 3)
    page = "<div>" * 20000 + "x" + "</div>" * 20000
    web.add(
        PAGE,
        httpx.Response(
            200, content=page.encode(), headers={"content-type": "text/html"}
        ),
    )
    t0 = time.monotonic()
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert result.is_error
    assert "took longer than 3 s" in result.content[0].text  # type: ignore[union-attr]
    assert time.monotonic() - t0 < 8


async def test_hung_extraction_does_not_block_other_requests(
    web: FakeWeb,
    monkeypatch: pytest.MonkeyPatch,
    fake_nodes: dict[str, Path],
    tmp_path: Path,
) -> None:
    """a page stuck in Node for the whole W leaves the server responsive."""
    monkeypatch.setattr(server_module, "_WORKER_TIMEOUT", 8)  # Node gets 8 - 5 = 3 s
    sleeper = tuple(node_argv(fake_nodes["sleeper"], str(tmp_path / "pid")))
    monkeypatch.setattr(_worker, "find_node", lambda: sleeper)
    html = {"content-type": "text/html"}
    web.add(PAGE, httpx.Response(200, content=ARTICLE.encode(), headers=html))
    plain = "https://example.com/plain.txt"
    web.add(
        plain,
        httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"}),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        hung = asyncio.create_task(session.call_tool("fetch", {"url": PAGE}))
        await asyncio.sleep(1)  # the worker is now waiting on the sleeping "node"
        t0 = time.monotonic()
        await session.list_tools()
        quick = await session.call_tool("fetch", {"url": plain})
        assert time.monotonic() - t0 < 1
        assert not quick.is_error and not hung.done()
        result = await hung
    assert not result.is_error  # Node gave up within the budget; pure-Python answered


async def test_six_concurrent_html_fetches_share_four_workers(
    web: FakeWeb, procs: list[Any]
) -> None:
    """six real workers through the semaphore of four, all answered."""
    web.add(
        PAGE,
        httpx.Response(
            200, content=ARTICLE.encode(), headers={"content-type": "text/html"}
        ),
    )
    async with connect(ignore_robots_txt=True, use_readability_js=False) as (
        session,
        _,
    ):
        results = await asyncio.gather(
            *(session.call_tool("fetch", {"url": PAGE}) for _ in range(6))
        )
    assert all(not r.is_error for r in results)
    assert len(procs) == 6


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_node_lookup_uses_absolute_path_entries_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """a relative PATH entry (the working directory included) is never searched."""
    name = "node.exe" if sys.platform == "win32" else "node"
    (tmp_path / "rel").mkdir()
    (tmp_path / "abs").mkdir()
    _executable(tmp_path / "rel" / name)
    _executable(tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["rel", ".", "", str(tmp_path / "abs")]))
    assert _worker._which_node() is None
    found = _executable(tmp_path / "abs" / name)
    assert _worker._which_node() == str(found)


def test_node_lookup_ignores_cmd_and_bat_shims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _executable(tmp_path / "node.cmd")
    _executable(tmp_path / "node.bat")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _worker._which_node() is None
    executable = _executable(
        tmp_path / ("node.exe" if sys.platform == "win32" else "node")
    )
    assert _worker._which_node() == str(executable)


@pytest.fixture
def fake_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A provisioned readabilipy javascript/ dir under tmp_path (hidden lockfile and the
    stamp the provisioning script writes), a node on PATH, and a probe that records its calls."""
    javascript = tmp_path / "readabilipy" / "javascript"
    (javascript / "node_modules").mkdir(parents=True)
    lock = b'{"lockfileVersion": 3}'
    (javascript / "node_modules" / ".package-lock.json").write_bytes(lock)
    (javascript / ".fetch-node-deps.sha256").write_text(
        hashlib.sha256(lock).hexdigest() + "\n"
    )
    monkeypatch.setattr(
        _worker.readabilipy, "__file__", str(javascript.parent / "__init__.py")
    )
    monkeypatch.setattr(_worker, "_which_node", lambda: "/x/node")
    _worker.find_node.cache_clear()
    yield javascript
    _worker.find_node.cache_clear()


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def run(argv: Any, **kw: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b'"<p>probe</p>"', b"")

    monkeypatch.setattr(_worker.subprocess, "run", run)
    return calls


def test_find_node_probes_once_and_never_npm(
    fake_install: Path, probes: list[Any]
) -> None:
    a, b = _worker.find_node(), _worker.find_node()
    assert a == b and a is not None and len(probes) == 1
    assert a[0] == "/x/node" and not any("npm" in x for x in a)


def test_find_node_is_none_without_node_modules(
    fake_install: Path, probes: list[Any]
) -> None:
    (fake_install / "node_modules" / ".package-lock.json").unlink()
    (fake_install / "node_modules").rmdir()
    assert _worker.find_node() is None and probes == []


@pytest.mark.parametrize(
    "drift", ["stamp missing", "lockfile missing", "mismatch", "no newline"]
)
def test_unprovisioned_node_modules_fall_back_with_one_warning(
    fake_install: Path,
    probes: list[Any],
    capsys: pytest.CaptureFixture[str],
    drift: str,
) -> None:
    """node_modules that are not what the provisioning script provisioned mean no Node, never npm."""
    stamp = fake_install / ".fetch-node-deps.sha256"
    lock = fake_install / "node_modules" / ".package-lock.json"
    if drift == "stamp missing":
        stamp.unlink()
    elif drift == "lockfile missing":
        lock.unlink()
    elif drift == "mismatch":
        lock.write_bytes(b'{"lockfileVersion": 3, "drifted": true}')
    else:
        stamp.write_text(stamp.read_text().strip())
    assert _worker.find_node() is None and _worker.find_node() is None
    assert probes == []
    err = capsys.readouterr().err
    assert err.count("scripts/provision_node_deps.py") == 1 and "npm" not in err.split()


def test_windows_worker_runs_the_base_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """never the venv launcher, which would start the real worker outside the job."""
    monkeypatch.setattr(server_module.sys, "platform", "win32")
    monkeypatch.setattr(server_module.sys, "executable", r"C:\v\Scripts\python.exe")
    monkeypatch.setattr(
        server_module.sys, "_base_executable", r"C:\Python315\python.exe", raising=False
    )
    argv, env = _worker_launch()
    assert argv == [r"C:\Python315\python.exe", "-I", "-m", "mcp_server_fetch._worker"]
    assert env is not None and env["__PYVENV_LAUNCHER__"] == r"C:\v\Scripts\python.exe"


def test_outside_windows_the_worker_gets_a_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.sys, "platform", "linux")
    monkeypatch.setenv("FETCH_API_KEY", "not-for-the-worker")
    argv, env = _worker_launch()
    assert argv == [sys.executable, "-I", "-m", "mcp_server_fetch._worker"]
    assert env is not None and "PATH" in env
    assert "FETCH_API_KEY" not in env


async def test_no_readability_js_never_probes(
    web: FakeWeb,
    monkeypatch: pytest.MonkeyPatch,
    worker_in_process: list[dict[str, Any]],
) -> None:
    def boom() -> None:
        raise AssertionError("probed")

    monkeypatch.setattr(_worker, "find_node", boom)
    web.add(
        PAGE,
        httpx.Response(
            200, content=ARTICLE.encode(), headers={"content-type": "text/html"}
        ),
    )
    async with connect(ignore_robots_txt=True, use_readability_js=False) as (
        session,
        _,
    ):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert not result.is_error
    assert worker_in_process and all(r["node"] is None for r in worker_in_process)


def test_extract_content_from_html_is_sync() -> None:
    assert not inspect.iscoroutinefunction(server_module.extract_content_from_html)


def test_hidden_numbering_is_stable_across_parses() -> None:
    page = "<div hidden>a<div hidden>b</div></div><nav hidden>n</nav><p hidden>xyz</p>"
    _, candidates = _worker._prune(_worker._parse(page), remove=False)
    assert [n for n, _ in candidates] == [0, 2]  # nav (1) is furniture
    again = _worker._parse(page)
    _worker._prune(again, remove=False, unhide=frozenset({2}))
    p = again.find(f".//{_worker._HTML}p")
    nav = again.find(f".//{_worker._HTML}nav")
    assert p is not None and "hidden" not in p.attrib
    assert nav is not None and "hidden" in nav.attrib


def _imports_and_calls(path: Path) -> tuple[set[str], set[str], set[object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    calls: set[str] = set()
    constants: set[object] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            calls.add(
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            )
        elif isinstance(node, ast.Constant):
            constants.add(node.value)
    return modules, calls, constants


def test_parsing_stays_out_of_the_server() -> None:
    """Inspect imports and calls, not text (comments may name them). decode_page's
    1 KiB meta-charset scan uses the stdlib HTMLParser and may stay."""
    modules, calls, constants = _imports_and_calls(SRC / "server.py")
    assert not modules & {
        "bs4",
        "html5lib",
        "markdownify",
        "readabilipy",
        "multiprocessing",
    }
    assert "BeautifulSoup" not in calls and "html.parser" not in constants


def test_worker_spawn_is_isolated() -> None:
    argv, _ = _worker_launch()
    assert argv[1:] == ["-I", "-m", "mcp_server_fetch._worker"]


def test_worker_launch_does_not_inherit_server_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FETCH_TEST_SECRET", "do-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    _, env = _worker_launch()
    assert "FETCH_TEST_SECRET" not in env
    assert "PATH" in env


def test_windows_limits_are_owned_by_the_job_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_worker.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "resource", None)
    _worker._limit_self(100)


def test_limit_self_never_loosens_an_operator_limit(
    monkeypatch: pytest.MonkeyPatch,
    posix_resource: Any,
) -> None:
    seen: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(kind: int) -> tuple[int, int]:
        return (10, 20)

    def setrlimit(kind: int, limits: tuple[int, int]) -> None:
        seen.append((kind, limits))

    monkeypatch.setattr(posix_resource, "getrlimit", getrlimit)
    monkeypatch.setattr(posix_resource, "setrlimit", setrlimit)
    _worker._limit_self(100)
    assert seen and all(soft <= 10 for _, (soft, _) in seen)


def test_fake_node_memory_failure_falls_back_not_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "node.py"
    fake.write_text(
        "raise MemoryError('extractor allocation failed')\n", encoding="utf-8"
    )

    async def run() -> dict[str, Any]:
        return await server_module._run_worker(
            req(
                "<article><h1>Hello</h1><p>Content survives the memory hog.</p></article>",
                node=node_argv(fake),
            ),
            server_module._new_slots(),
            time.monotonic() + 30,
        )

    result = asyncio.run(run())
    assert "Hello" in result["content"]


def test_windows_node_lookup_rejects_drive_relative_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ntpath = __import__("ntpath")
    monkeypatch.setattr(_worker.sys, "platform", "win32")
    monkeypatch.setattr(_worker.os, "path", ntpath)
    monkeypatch.setattr(_worker.os, "pathsep", ";")
    monkeypatch.setattr(ntpath, "isfile", lambda p: p.endswith("node.exe"))
    monkeypatch.setattr(_worker.os, "access", lambda p, mode: True)
    monkeypatch.setenv("PATH", "C:tools;\\tools")
    assert _worker._which_node() is None
    monkeypatch.setenv("PATH", "C:\\tools;\\\\server\\share")
    assert _worker._which_node() == "C:\\tools\\node.exe"


def test_parent_text_pipeline_holds_a_running_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRunning(asyncio.Semaphore):
        acquired = False

        async def acquire(self) -> Literal[True]:
            self.acquired = True
            return await super().acquire()

    running = FakeRunning()
    monkeypatch.setattr(
        server_module, "_text_pipeline", lambda *a, **k: ("plain", {}, "")
    )
    asyncio.run(
        server_module._render(
            "plain",
            "text/plain",
            page_url=PAGE,
            raw=False,
            sanitize=True,
            include_links=False,
            node=None,
            slots=server_module._Slots(asyncio.Semaphore(), running),
            deadline=None,
        )
    )
    assert running.acquired


def test_worker_runs_from_a_path_with_spaces_and_unicode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = Path(sys.prefix)
    shadow = tmp_path / "venv with space-é"
    (shadow / "bin").mkdir(parents=True)
    (shadow / "pyvenv.cfg").write_bytes((prefix / "pyvenv.cfg").read_bytes())
    (shadow / "lib").symlink_to(prefix / "lib", target_is_directory=True)
    link = shadow / "bin" / "python"
    link.symlink_to(sys.executable)
    monkeypatch.setattr(server_module.sys, "executable", str(link))

    async def run() -> dict[str, Any]:
        return await server_module._spawn_worker(
            json.dumps(
                req("<article><p>Hello from Unicode path</p></article>")
            ).encode()
        )

    result = asyncio.run(run())
    assert "Hello from Unicode path" in result["content"]


def test_contain_is_the_first_statement_inside_its_try() -> None:
    tree = ast.parse(inspect.getsource(server_module._spawn_worker))
    containment = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(
            isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "job" for t in stmt.targets)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", "") == "_contain"
            for stmt in node.body
        )
    ]
    assert containment, "containment call not found"
    first = containment[0].body[0]
    assert isinstance(first, ast.Assign)
    assert isinstance(first.value, ast.Call)
    assert getattr(first.value.func, "id", "") == "_contain"
