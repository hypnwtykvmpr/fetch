"""README must agree with the package, CLI, tool schema and CI matrix."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
WORKFLOW = (ROOT.parents[1] / ".github/workflows/fetch-ci.yml").read_text(
    encoding="utf-8"
)


def section(name: str, next_name: str) -> str:
    start = README.index(name)
    end = README.index(next_name, start)
    return README[start:end]


def table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        if line.startswith("| `"):
            rows.append(
                [cell.strip().strip("`") for cell in line.strip("|").split("|")]
            )
    return rows


def main() -> int:
    problems: list[str] = []
    version = re.search(r'^version = "([^"]+)"', PYPROJECT, re.MULTILINE)
    assert version is not None
    if README.count(version.group(1)) < 1:
        problems.append("README does not name the pyproject version")

    for block in re.findall(r"```json\n(.*?)\n```", README, re.DOTALL):
        try:
            config = json.loads(block)
        except json.JSONDecodeError as exc:
            problems.append(f"invalid client JSON: {exc}")
            continue
        servers = config.get("mcpServers", config.get("servers", {}))
        entry = servers.get("fetch", {})
        if not entry.get("command") or not isinstance(entry.get("args"), list):
            problems.append(
                "client example must define fetch command and argument array"
            )
        if str(entry.get("command", "")).endswith(("uv", "uv.exe")) and not {
            "run",
            "--locked",
            "--no-dev",
            "--directory",
            "mcp-server-fetch",
        } <= set(entry.get("args", [])):
            problems.append("checkout client example does not use locked fork launch")
    for filename in ("README.md", "CONTRIBUTING.md"):
        document = (ROOT / filename).read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document):
            if (
                "://" not in target
                and not target.startswith("#")
                and not (ROOT / target.split("#", 1)[0]).is_file()
            ):
                problems.append(
                    f"broken local documentation link in {filename}: {target}"
                )

    node_package = json.loads(
        (ROOT / "scripts/readability-js/dependencies.json").read_text()
    )
    if node_package["engines"]["node"] not in README:
        problems.append("README Node version range differs from provisioning package")

    help_text = subprocess.run(
        [sys.executable, "-m", "mcp_server_fetch", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    actual_flags = set(re.findall(r"(--[a-z][a-z0-9-]+)", help_text)) - {"--help"}
    documented_flags = {
        row[0] for row in table_rows(section("## CLI options", "## Configuration"))
    }
    if actual_flags != documented_flags:
        problems.append(
            f"CLI flags drift: code={sorted(actual_flags)} docs={sorted(documented_flags)}"
        )

    async def schema() -> dict[str, object]:
        from mcp_server_fetch.server import create_server

        server = create_server(use_readability_js=False)
        tools = await server.list_tools()
        tool = next((t for t in tools if t.name == "fetch"), None)
        if tool is None:
            raise RuntimeError("fetch tool is missing from the server schema")
        return tool.input_schema

    schema_data = asyncio.run(schema())
    properties = cast(dict[str, dict[str, Any]], schema_data.get("properties", {}))
    required = set(cast(list[str], schema_data.get("required", [])))
    actual_params = set(properties)
    documented_params = {
        row[0] for row in table_rows(section("## Tool parameters", "## CLI options"))
    }
    if actual_params != documented_params:
        problems.append(
            f"tool parameter drift: code={sorted(actual_params)} docs={sorted(documented_params)}"
        )
    for name, prop in properties.items():
        if name not in required and "default" in prop:
            row_doc = next(
                (
                    row
                    for row in table_rows(
                        section("## Tool parameters", "## CLI options")
                    )
                    if row[0] == name
                ),
                None,
            )
            if row_doc is None:
                problems.append(f"parameter not documented: {name}")
                continue
            if row_doc[1].lower() != str(prop["default"]).lower():
                problems.append(
                    f"default drift for {name}: code={prop['default']} docs={row_doc[1]}"
                )

    for token in ("pip", "npx", "mcp/fetch", "uvx mcp-server-fetch"):
        if re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", README):
            problems.append(f"forbidden README token: {token}")

    workflow_data = yaml.safe_load(WORKFLOW)
    cfg = workflow_data["jobs"]["test"]["strategy"]["matrix"]
    expected = {
        (os_name, str(py), bool(node))
        for os_name in cfg["os"]
        for py in cfg["python"]
        for node in cfg["node"]
    }
    expected |= {
        (row["os"], str(row["python"]), bool(row["node"]))
        for row in cfg.get("include", [])
    }
    documented: set[tuple[str, str, bool]] = set()
    for line in README.splitlines():
        m = re.match(
            r"\| (Linux|Windows|macOS) \(`([^`]+)`\) \| [^|]+ \| ([^|]+) \| ([^|]+) \|",
            line,
        )
        if not m:
            continue
        os_name = m.group(2)
        pythons = {p.strip() for p in m.group(3).split(",") if p.strip()}
        cell = m.group(4).lower()
        both = "with and without" in cell
        with_node = both or "with node" in cell
        without_node = both or "without node" in cell
        modes = {True: with_node, False: without_node}
        documented |= {
            (os_name, py, node)
            for py in pythons
            for node in (True, False)
            if modes[node]
        }
    if expected != documented:
        problems.append(
            f"platform table drift: missing={sorted(expected - documented)} "
            f"extra={sorted(documented - expected)}"
        )
    if "linux/amd64" not in README or "docker" not in workflow_data["jobs"]:
        problems.append("Docker platform row is not reflected in README/CI")

    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
