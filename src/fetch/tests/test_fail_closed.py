"""No broad except that swallows; no raw-page fallback; secrets only in _envelope."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mcp_server_fetch"
FILES = [SRC / "server.py", SRC / "_worker.py", SRC / "_textsan.py"]


def _names(node: ast.expr | None) -> list[str]:
    if node is None:
        return ["<bare>"]
    if isinstance(node, ast.Tuple):
        return [n for e in node.elts for n in _names(e)]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    return []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_broad_excepts_reraise_and_never_return_the_page(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        if {"<bare>", "Exception", "BaseException"} & set(_names(handler.type)):
            assert any(
                isinstance(n, ast.Raise) for b in handler.body for n in ast.walk(b)
            ), (path.name, handler.lineno)
        for stmt in (
            n for b in handler.body for n in ast.walk(b) if isinstance(n, ast.Return)
        ):
            assert not (
                isinstance(stmt.value, ast.Name)
                and stmt.value.id in {"page_raw", "html"}
            ), (path.name, stmt.lineno)


def test_secrets_is_used_only_in_envelope() -> None:
    tree = ast.parse((SRC / "server.py").read_text(encoding="utf-8"))
    for fn in (
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        uses = [
            n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "secrets"
        ]
        assert not uses or fn.name == "_envelope", fn.name
    for other in (SRC / "_worker.py", SRC / "_textsan.py"):
        assert "secrets" not in other.read_text(encoding="utf-8")
