"""Reproducible lint, tests, coverage and package checks for Fetch."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv") or "uv"


def run(
    argv: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> None:
    print("+", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env, input=stdin, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def lint() -> None:
    run([UV, "lock", "--check"])
    run([UV, "run", "--locked", "ruff", "check", "."])
    run([UV, "run", "--locked", "ruff", "format", "--check", "."])
    run([UV, "run", "--locked", "pyright"])
    run(
        [
            UV,
            "run",
            "--locked",
            "python",
            "scripts/gen_emoji_ranges.py",
            "scripts/data/emoji-data.txt",
            "--check",
        ]
    )
    run([UV, "run", "--locked", "python", "ci/check_docs.py"])


def _nodefree_env() -> dict[str, str]:
    # This is a query, including inside Actions. The script's export mode
    # writes the next step's environment file instead of returning PATH.
    query_env = dict(os.environ)
    query_env.pop("GITHUB_ENV", None)
    path = subprocess.check_output(
        [sys.executable, str(ROOT / "ci/strip_node_path.py")], text=True, env=query_env
    ).strip()
    return {**os.environ, "PATH": path}


def test_stage(
    *, node: bool, python: str | None = None, venv: Path | None = None
) -> None:
    extra = {"UV_PROJECT_ENVIRONMENT": str(venv)} if venv else {}
    if python:
        run([UV, "sync", "--locked", "--python", python], env={**os.environ, **extra})
    argv = [UV, "run", "--locked"]
    if python:
        argv += ["--python", python]
    argv += ["pytest", "-q", "-p", "no:cacheprovider"]
    env = dict(os.environ)
    if node:
        env["REQUIRE_NODE"] = "1"
    else:
        env = _nodefree_env()
        argv += ["-m", "not node"]
    run(argv, env={**env, **extra})


def coverage_stage() -> None:
    run([UV, "sync", "--locked"])
    run([UV, "run", "--locked", "python", "scripts/provision_node_deps.py"])
    with tempfile.TemporaryDirectory(prefix="fetch-cov-") as raw:
        temp = Path(raw)
        cov = temp / "coverage.json"
        run(
            [
                UV,
                "run",
                "--locked",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--cov",
                f"--cov-report=json:{cov}",
            ],
            env={
                **os.environ,
                "COVERAGE_FILE": str(temp / "coverage.data"),
                "REQUIRE_NODE": "1",
            },
        )
        run([UV, "run", "--locked", "python", "ci/coverage_floor.py", str(cov)])


def launch_check_argv(argv: list[str]) -> None:
    run([UV, "run", "--locked", "python", "ci/launch_check.py", "--", *argv])


def package_stage() -> None:
    with tempfile.TemporaryDirectory(prefix="fetch-package-") as raw:
        temp = Path(raw)
        dist = temp / "dist"
        run(
            [
                UV,
                "build",
                "-o",
                str(dist),
                "-b",
                str(ROOT / "ci/build-constraints.txt"),
                "--require-hashes",
            ]
        )
        run([UV, "run", "--locked", "python", "ci/check_dist.py", "inspect", str(dist)])
        constraints = temp / "constraints.txt"
        run(
            [
                UV,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "-o",
                str(constraints),
            ]
        )
        script = shutil.which("mcp-server-fetch", path=str(Path(sys.executable).parent))
        if script is None:
            raise SystemExit("package: console script not found in interpreter bin dir")
        launch_check(Path(script))
        wheel = next(dist.glob("*.whl"))
        launch_check_argv(
            [
                shutil.which("uvx") or "uvx",
                "--isolated",
                "--python",
                "3.15",
                "-c",
                str(constraints),
                "--from",
                str(wheel),
                "mcp-server-fetch",
            ]
        )
        tool_dir, tool_bin = temp / "tools", temp / "bin"
        run(
            [
                UV,
                "tool",
                "install",
                "--python",
                "3.15",
                "-c",
                str(constraints),
                str(wheel),
            ],
            env={
                **os.environ,
                "UV_TOOL_DIR": str(tool_dir),
                "UV_TOOL_BIN_DIR": str(tool_bin),
            },
        )
        launch_check(
            tool_bin
            / ("mcp-server-fetch.exe" if os.name == "nt" else "mcp-server-fetch")
        )


def docker_stage() -> None:
    if sys.platform == "win32":
        raise SystemExit("docker: run on Linux or macOS; Windows Docker may start WSL")
    import io
    import tarfile

    with tempfile.TemporaryDirectory(prefix="fetch-docker-") as raw:
        temp = Path(raw)
        archive = subprocess.check_output(
            ["git", "archive", "--format=tar", "HEAD:src/fetch"], cwd=ROOT.parents[1]
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
            tf.extractall(temp, filter="data")
        run(
            [
                UV,
                "run",
                "--locked",
                "python",
                "ci/docker_context_check.py",
                str(temp),
            ]
        )
        launch_check_argv(
            ["docker", "run", "-i", "--rm", "mcp-server-fetch-fork:canary"]
        )
        # The loopback fixture and the MCP child must share a network namespace.
        run(
            [
                "docker",
                "run",
                "-i",
                "--rm",
                "--entrypoint",
                "python",
                "mcp-server-fetch-fork:canary",
                "-",
                "--",
                "mcp-server-fetch",
                "--allow-private-ips",
                "--ignore-robots-txt",
            ],
            stdin=(ROOT / "ci/raw_stdio_check.py").read_bytes(),
        )


def fast() -> None:
    lint()
    test_stage(node=False)


def full() -> None:
    fast()
    coverage_stage()
    package_stage()


def launch_check(script: Path) -> None:
    run([UV, "run", "--locked", "python", "ci/launch_check.py", "--", str(script)])


def docs() -> None:
    run([UV, "run", "--locked", "python", "ci/check_docs.py"])


def main() -> None:
    commands = {
        "lint": lint,
        "fast": fast,
        "full": full,
        "docs": docs,
        "coverage": coverage_stage,
        "package": package_stage,
        "docker": docker_stage,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in {*commands, "test"}:
        raise SystemExit(
            "usage: check.py lint|fast|full|docs|test --node/--no-node|coverage|package|docker"
        )
    if sys.argv[1] == "test":
        if len(sys.argv) != 3 or sys.argv[2] not in {"--node", "--no-node"}:
            raise SystemExit("test requires --node or --no-node")
        test_stage(node=sys.argv[2] == "--node")
    else:
        if len(sys.argv) != 2:
            raise SystemExit("unexpected arguments")
        commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
