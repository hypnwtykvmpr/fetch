"""What the extraction path runs, and when: the server probes Node once at startup,
never installs packages, and passes its choice explicitly to every worker request."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import httpx

from mcp_server_fetch import _worker

from .harness import FakeWeb, connect

PAGE = "https://example.com/page"
ROBOTS = "https://example.com/robots.txt"

ARTICLE_HTML = """<html><head><title>Test Page</title></head><body>
<article>
<h1>Hello World</h1>
<p>This is a test paragraph with enough words to count as the main content.</p>
<h2>Sub heading</h2>
</article>
</body></html>"""


def text_of(result: Any) -> str:
    data = result.model_dump(
        by_alias=True, mode="json", exclude_none=True, exclude_unset=True
    )
    [content] = data["content"]
    assert content["type"] == "text"
    return content["text"]


async def test_a_fetch_never_installs_packages_nor_probes_node_when_off(
    web: FakeWeb, monkeypatch: Any, worker_in_process: list[dict[str, Any]]
) -> None:
    probes: list[int] = []
    monkeypatch.setattr(_worker, "find_node", lambda: probes.append(1))
    web.add(
        ROBOTS, httpx.Response(404, content=b"", headers={"content-type": "text/plain"})
    )
    web.add(
        PAGE,
        httpx.Response(
            200, content=ARTICLE_HTML.encode(), headers={"content-type": "text/html"}
        ),
    )
    async with connect() as (session, _):
        results = [await session.call_tool("fetch", {"url": PAGE}) for _ in range(2)]
    assert len(probes) == 1
    assert all(r["node"] is None for r in worker_in_process)
    assert "Sub heading" in text_of(results[-1])


def _code_strings_and_names(path: Path) -> set[str]:
    """String constants that are not docstrings, and every name and attribute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.add(node.value)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def test_a_fetch_with_readability_on_runs_no_npm_install() -> None:
    """code, not prose. Only server.py and _worker.py run anything; __init__'s
    --help text and docstrings may name npm."""
    src = Path(__file__).resolve().parents[1] / "src" / "mcp_server_fetch"
    for path in (src / "server.py", src / "_worker.py"):
        for value in _code_strings_and_names(path):
            words = value.lower().replace("`", " ").split()
            assert "npm" not in words and "npx" not in words, (path.name, value[:80])
            assert value not in ("have_node", "run_npm_install"), (path.name, value)
