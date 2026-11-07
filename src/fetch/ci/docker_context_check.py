"""Prove the Docker build context and the built image hold no private files.

  uv run --locked python ci/docker_context_check.py <context-dir> [--image TAG]

Plants canaries (ci/check_dist.py plant) into <context-dir>: run it on a disposable
copy or a CI checkout, NEVER on a working tree that holds real private files.

Exits 0 only when the context equals the allowlist exactly, no canary byte is in
the context or in any image layer, and uid/python/node match the Dockerfile.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

CANARY = b"FETCH-PRIVACY-CANARY"
CONTEXT_FILES = {"pyproject.toml", "uv.lock", ".python-version", "README.md", "LICENSE"}
EXCLUDED_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".pyc", ".pyo", ".pyd"}
LAYER_BLOB = re.compile(r"blobs/sha256/[0-9a-f]{64}")
LEGACY_LAYER = re.compile(r"[0-9a-f]{64}/layer\.tar")


def expected_context(ctx: Path) -> set[str]:
    """The allowlist in .dockerignore, restricted to files that exist."""
    expected = set(CONTEXT_FILES)
    for path in (ctx / "src").rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ctx)
        if "__pycache__" in rel.parts:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        expected.add(rel.as_posix())
    return expected


def docker(
    *args: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    if sys.platform == "win32":
        raise SystemExit("docker: run on Linux or macOS; Windows Docker may start WSL")
    return subprocess.run(
        ["docker", *args], check=False, input=stdin, capture_output=True
    )


def detail(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stderr.decode("utf-8", "replace").strip()[-400:]


def scan_tar(
    data: bytes, label: str, problems: list[str], *, nested: bool = False
) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    problems.append(f"unreadable member {label}:{member.name}")
                    continue
                payload = handle.read()
                if CANARY in payload:
                    problems.append(f"canary bytes in {label}:{member.name}")
                if nested:
                    continue
                is_layer = LAYER_BLOB.fullmatch(member.name) or LEGACY_LAYER.fullmatch(
                    member.name
                )
                if not is_layer:
                    continue
                # Config, manifest and index blobs are JSON; other blob paths are layers.
                if payload.lstrip()[:1] == b"{":
                    try:
                        json.loads(payload)
                    except ValueError:
                        problems.append(
                            f"unscannable layer {label}:{member.name}: "
                            "neither JSON nor a tar archive"
                        )
                    continue
                scan_tar(payload, f"{label}:{member.name}", problems, nested=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable layer is a fail-closed problem
        problems.append(f"unscannable layer {label}: {type(exc).__name__}: {exc}")


def emit(summary: dict[str, object], problems: list[str]) -> int:
    summary["problems"] = problems
    print(json.dumps(summary, indent=2))
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    parser.add_argument("--image", default="mcp-server-fetch-fork:canary")
    args = parser.parse_args()

    ctx = args.context.resolve()
    summary: dict[str, object] = {
        "context": str(ctx),
        "image": args.image,
        "context_files": [],
        "leaked": [],
        "missing": [],
        "image_id": None,
        "uid": None,
        "python": None,
        "node_present": None,
    }
    problems: list[str] = []
    if not ctx.is_dir():
        problems.append(f"context directory does not exist: {ctx}")
        return emit(summary, problems)

    expected = expected_context(ctx)
    planter = Path(__file__).with_name("check_dist.py")
    planted = subprocess.run(
        [sys.executable, str(planter), "plant", str(ctx)],
        check=False,
        capture_output=True,
        text=True,
    )
    if planted.returncode != 0:
        problems.append(f"canary planting failed: {planted.stderr.strip()}")
        return emit(summary, problems)

    if shutil.which("docker") is None:
        problems.append("docker CLI not found")
        return emit(summary, problems)

    with tempfile.TemporaryDirectory(prefix="docker-context-") as tmp:
        tmp_path = Path(tmp)
        dump_dir = tmp_path / "ctx"
        dumped = docker(
            "buildx",
            "build",
            "--no-cache",
            "-f",
            "-",
            "-o",
            f"type=local,dest={dump_dir}",
            str(ctx),
            stdin=b"FROM scratch\nCOPY . /\n",
        )
        if dumped.returncode != 0:
            problems.append(f"context dump failed: {detail(dumped)}")
        else:
            got = {
                path.relative_to(dump_dir).as_posix()
                for path in dump_dir.rglob("*")
                if path.is_file()
            }
            leaked = sorted(got - expected)
            missing = sorted(expected - got)
            summary["context_files"] = sorted(got)
            summary["leaked"] = leaked
            summary["missing"] = missing
            problems.extend(f"leaked into context: {name}" for name in leaked)
            problems.extend(f"missing from context: {name}" for name in missing)
            for path in dump_dir.rglob("*"):
                if path.is_file() and CANARY in path.read_bytes():
                    rel = path.relative_to(dump_dir).as_posix()
                    problems.append(f"canary bytes in context: {rel}")

        built = docker("build", "-t", args.image, str(ctx))
        if built.returncode != 0:
            problems.append(f"image build failed: {detail(built)}")
            return emit(summary, problems)

        inspected = docker("image", "inspect", "--format", "{{.Id}}", args.image)
        if inspected.returncode == 0:
            summary["image_id"] = inspected.stdout.decode("utf-8", "replace").strip()
        else:
            problems.append(f"image inspect failed: {detail(inspected)}")

        tar_path = tmp_path / "image.tar"
        saved = docker("save", "-o", str(tar_path), args.image)
        if saved.returncode != 0:
            problems.append(f"docker save failed: {detail(saved)}")
        else:
            scan_tar(tar_path.read_bytes(), "image.tar", problems)

        uid = docker("run", "--rm", "--entrypoint", "id", args.image, "-u")
        if uid.returncode != 0:
            problems.append(f"uid check failed: {detail(uid)}")
        else:
            uid_value = uid.stdout.decode("utf-8", "replace").strip()
            summary["uid"] = uid_value
            if uid_value != "10001":
                problems.append(f"uid is {uid_value}, expected 10001")

        node = docker(
            "run", "--rm", "--entrypoint", "sh", args.image, "-c", "command -v node"
        )
        node_present = node.returncode == 0
        summary["node_present"] = node_present
        if node_present:
            found = node.stdout.decode("utf-8", "replace").strip()
            problems.append(f"node present in image: {found}")

        python = docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            args.image,
            "-c",
            "import sys; print(sys.version.split()[0])",
        )
        if python.returncode != 0:
            problems.append(f"python check failed: {detail(python)}")
        else:
            version = python.stdout.decode("utf-8", "replace").strip()
            summary["python"] = version
            if version != "3.15.2":
                problems.append(f"python is {version}, expected 3.15.2")

    return emit(summary, problems)


if __name__ == "__main__":
    sys.exit(main())
