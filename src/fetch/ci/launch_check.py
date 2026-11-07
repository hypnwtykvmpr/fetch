"""Launch a command as an MCP stdio server and check both protocol eras.

uv run --locked python ci/launch_check.py [--expect-version V] -- COMMAND ARGS...
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
ERAS = (("legacy", "2025-11-25"), ("auto", "2026-07-28"))


async def check(exe: str, rest: list[str], expected: str) -> None:
    cwd = tempfile.mkdtemp(prefix="fetch launch ü ")
    env = dict(os.environ)
    for mode, protocol in ERAS:
        with anyio.fail_after(300):
            params = StdioServerParameters(command=exe, args=rest, cwd=cwd, env=env)
            started = time.monotonic()
            async with Client(params, mode=mode) as client:
                startup = round(time.monotonic() - started, 2)
                if client.session.protocol_version != protocol:
                    raise RuntimeError(
                        f"{mode}: protocol {client.session.protocol_version!r}, "
                        f"expected {protocol!r}"
                    )
                info = client.server_info
                if info is None or info.name != "mcp-fetch":
                    raise RuntimeError(f"{mode}: server info {info!r}")
                if info.version != expected:
                    raise RuntimeError(
                        f"{mode}: version {info.version!r}, expected {expected!r}"
                    )
                tools = [t.name for t in (await client.list_tools()).tools]
                if tools != ["fetch"]:
                    raise RuntimeError(f"{mode}: tools {tools!r}")
                prompts = [p.name for p in (await client.list_prompts()).prompts]
                if prompts != ["fetch"]:
                    raise RuntimeError(f"{mode}: prompts {prompts!r}")
                print(
                    json.dumps(
                        {
                            "mode": mode,
                            "protocol": protocol,
                            "server_info": info.model_dump(),
                            "startup_s": startup,
                        }
                    )
                )


def main() -> int:
    argv = sys.argv[1:]
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    head, command = argv[:split], argv[split + 1 :]
    expected = None
    if head[:1] == ["--expect-version"] and len(head) == 2:
        expected = head[1]
    elif head:
        print(__doc__, file=sys.stderr)
        return 2
    if expected is None:
        expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
    if "+fork." not in expected:
        print(f"{expected!r} lacks the +fork. marker", file=sys.stderr)
        return 2
    exe = shutil.which(command[0]) if command else None
    if exe is None:
        print(f"command not found: {command[:1]}", file=sys.stderr)
        return 1
    anyio.run(check, exe, command[1:], expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
