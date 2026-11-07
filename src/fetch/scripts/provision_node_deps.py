"""Install readabilipy's Readability.js dependencies from the committed lock.

Run from a checkout, in the environment that runs the server:
    uv run --locked python scripts/provision_node_deps.py
Needs node and npm on PATH (Node satisfying scripts/readability-js/dependencies.json "engines").
Never runs at request time or in test fixtures; the server checks the provisioning stamp
and probes extraction at startup.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import readabilipy

LOCK_DIR = Path(__file__).resolve().parent / "readability-js"
# Must stay the directory _worker.find_node() checks.
TARGET = Path(readabilipy.__file__).resolve().parent / "javascript"
# Drift check: sha256 of npm's hidden lockfile, recorded here after a
# successful `npm ci`. find_node recomputes it and refuses Node mode on mismatch.
HIDDEN_LOCK = TARGET / "node_modules" / ".package-lock.json"
STAMP = TARGET / ".fetch-node-deps.sha256"
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _which_absolute(name: str, windows_name: str) -> str | None:
    """Find a tool only on absolute PATH entries; never search the working directory."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if os.name == "nt":
            if not (os.path.isabs(entry) and os.path.splitdrive(entry)[0]):
                continue
            candidate = os.path.join(entry, windows_name)
        else:
            if not os.path.isabs(entry):
                continue
            candidate = os.path.join(entry, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def main() -> int:
    # npm is npm.cmd on Windows: always call the resolved path, never the bare name.
    node = _which_absolute("node", "node.exe")
    npm = _which_absolute("npm", "npm.cmd")
    if node is None or npm is None:
        print(
            f"node and npm must be on PATH (node={node!r}, npm={npm!r})",
            file=sys.stderr,
        )
        return 2
    # A failed run must not leave a stamp that vouches for a half-installed tree.
    STAMP.unlink(missing_ok=True)
    # Source manifests use distinct names so repository package discovery does
    # not mistake this extraction dependency bundle for another server workspace.
    for source, name in (
        ("dependencies.json", "package.json"),
        ("dependencies-lock.json", "package-lock.json"),
    ):
        dest = TARGET / name
        # Break a uv hardlink to the shared cache before writing (Linux default link mode).
        dest.unlink(missing_ok=True)
        dest.write_bytes((LOCK_DIR / source).read_bytes())
    subprocess.run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=TARGET,
        check=True,
        timeout=600,
        creationflags=FLAGS,
        stdout=sys.stderr,
    )
    probe = "require('jsdom'); require('@mozilla/readability'); require('minimist')"
    subprocess.run(
        [node, "-e", probe],
        cwd=TARGET,
        check=True,
        timeout=60,
        creationflags=FLAGS,
    )
    digest = hashlib.sha256(HIDDEN_LOCK.read_bytes()).hexdigest()
    STAMP.write_bytes(f"{digest}\n".encode("ascii"))
    print(f"provisioned {TARGET}", file=sys.stderr)
    print(f"{STAMP.name}: {digest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
