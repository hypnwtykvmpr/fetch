"""Drive an MCP stdio server with raw JSON-RPC lines and check the wire.

  uv run --locked python ci/raw_stdio_check.py -- COMMAND ARGS...

The server must be started with --allow-private-ips --ignore-robots-txt (the page is on loopback).
Checks: stdout carries only JSON-RPC lines, Unicode survives, a page that decodes to a lone
surrogate still gets a result, cancellation leaves the server usable, and closing stdin ends
the process with exit code 0.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PAGE = (
    "<html><head><title>t</title></head><body><article><h1>Grüße</h1>"
    "<p>héllo – 日本語 ✓ 🙂</p><p>second paragraph of article text</p></article></body></html>"
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        ctype = "text/html; charset=utf-8"
        if self.path == "/slow":
            time.sleep(20)
            body = b"slow"
        elif self.path == "/surrogate":
            # a header codec that decodes these bytes to a lone surrogate.
            body = b"lone \\ud800 surrogate"
            ctype = "text/plain; charset=unicode_escape"
        else:
            body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "--" or len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    exe = shutil.which(argv[1])
    if exe is None:
        print(f"command not found: {argv[1]}", file=sys.stderr)
        return 1
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    cwd = tempfile.mkdtemp(prefix="fetch raw ü ")
    problems: list[str] = []
    timings: dict[str, float] = {}
    with open(os.path.join(cwd, "stderr.txt"), "wb") as err:
        proc = subprocess.Popen(
            [exe, *argv[2:]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            cwd=cwd,
            env=os.environ,
        )
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout
    messages: queue.Queue[dict[str, Any]] = queue.Queue()
    seen: dict[Any, dict[str, Any]] = {}
    lines = 0
    violations: list[str] = []

    def reader() -> None:
        nonlocal lines
        for raw in stdout:
            lines += 1
            try:
                msg = json.loads(raw.decode("utf-8"))
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    raise ValueError("not a JSON-RPC 2.0 object")
            except ValueError as exc:
                violations.append(f"{exc}: {raw[:80]!r}")
                continue
            messages.put(msg)

    threading.Thread(target=reader, daemon=True).start()

    def send(msg: dict[str, Any]) -> None:
        try:
            stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
            stdin.flush()
        except OSError as exc:
            problems.append(f"cannot write to the server (exited?): {exc!r}")

    def wait_for(request_id: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while request_id not in seen:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = messages.get(timeout=remaining)
            except queue.Empty:
                return None
            if "id" in msg and ("result" in msg or "error" in msg):
                seen[msg["id"]] = msg
        return seen[request_id]

    t0 = time.monotonic()
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "0"},
            },
        }
    )
    if wait_for(1, 120) is None:
        problems.append("no initialize response")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    timings["initialize"] = time.monotonic() - t0

    t0 = time.monotonic()
    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "fetch",
                "arguments": {"url": f"http://127.0.0.1:{port}/page"},
            },
        }
    )
    resp = wait_for(2, 120)
    timings["fetch"] = time.monotonic() - t0
    if resp is None:
        problems.append("no fetch response")
    else:
        result = resp.get("result", {})
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            problems.append(f"fetch isError: {text[:200]}")
        for needle in ("Grüße", "日本語", "🙂"):
            if needle not in text:
                problems.append(f"fetch text lacks {needle!r}")

    send(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "fetch",
                "arguments": {"url": f"http://127.0.0.1:{port}/surrogate"},
            },
        }
    )
    resp = wait_for(5, 120)
    if resp is None or "result" not in resp:
        problems.append(f"lone-surrogate page: no result: {resp!r:.200}")

    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "fetch",
                "arguments": {"url": f"http://127.0.0.1:{port}/slow"},
            },
        }
    )
    time.sleep(0.5)
    t0 = time.monotonic()
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 3, "reason": "raw check"},
        }
    )
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    resp = wait_for(4, 10)
    timings["after_cancel"] = time.monotonic() - t0
    if resp is None:
        problems.append("tools/list after cancellation did not answer within 10 s")
    else:
        names = [t.get("name") for t in resp.get("result", {}).get("tools", [])]
        if names != ["fetch"]:
            problems.append(f"tools/list after cancellation: {names}")
    late = wait_for(3, 2)
    if (
        late is not None
        and late.get("result", {}).get("content")
        and not late["result"].get("isError")
    ):
        problems.append("cancelled request still returned a successful result")

    with contextlib.suppress(OSError):
        stdin.close()
    try:
        code = proc.wait(timeout=15)
        if code != 0:
            problems.append(f"exit code {code}")
    except subprocess.TimeoutExpired:
        proc.kill()
        problems.append("server did not exit after stdin closed")
    problems += [f"stdout violation: {v}" for v in violations]
    print(
        json.dumps(
            {
                "lines": lines,
                "violations": len(violations),
                "timings": timings,
                "problems": problems,
            }
        )
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
