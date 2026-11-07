"""Print (or export to $GITHUB_ENV) a PATH without Node.js, npm, npx or corepack.

  uv run --locked python ci/strip_node_path.py

Drops every PATH directory that contains one of the four tools, and the active
virtualenv's executable directory. With GITHUB_ENV set (CI) it requires uv, uvx
and git to stay reachable and appends PATH=<new> there; otherwise it prints the new
PATH, for local use. Removed directories go to stderr.
"""

from __future__ import annotations

import os
import shutil
import sys

TOOLS = ("node", "npm", "npx", "corepack")


def main() -> int:
    venv = os.environ.get("VIRTUAL_ENV")
    venv_bin = (
        os.path.normcase(os.path.join(venv, "Scripts" if os.name == "nt" else "bin"))
        if venv
        else None
    )
    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and venv_bin and os.path.normcase(entry) == venv_bin:
            print(f"removed (venv): {entry}", file=sys.stderr)
        elif entry and any(shutil.which(tool, path=entry) for tool in TOOLS):
            print(f"removed (node tools): {entry}", file=sys.stderr)
        else:
            kept.append(entry)
    new = os.pathsep.join(kept)
    left = [tool for tool in TOOLS if shutil.which(tool, path=new)]
    if left:
        print(f"still on the new PATH: {left}", file=sys.stderr)
        return 1
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        missing = [
            tool for tool in ("uv", "uvx", "git") if not shutil.which(tool, path=new)
        ]
        if missing:
            print(f"not reachable on the new PATH: {missing}", file=sys.stderr)
            return 1
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"PATH={new}\n")
    else:
        print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
