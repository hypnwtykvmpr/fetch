"""Per-file coverage floor: every measured file needs >= 90% lines and >= 90% branches.

uv run --locked python ci/coverage_floor.py coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FLOOR = 90.0
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/mcp_server_fetch"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    files = data["files"]
    problems: list[str] = []
    reported = {Path(name).as_posix() for name in files}
    for path in sorted(PACKAGE.glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if not any(r == rel or r.endswith("/" + rel) for r in reported):
            problems.append(f"{rel}: missing from the coverage report")
    for name, entry in sorted(files.items()):
        summary = entry["summary"]
        statements = summary["num_statements"]
        lines = (
            100.0 if statements == 0 else 100.0 * summary["covered_lines"] / statements
        )
        if "num_branches" not in summary:
            problems.append(f"{name}: no branch data")
            print(f"{lines:6.1f} {'n/a':>6} {name}")
            continue
        branches = summary["num_branches"]
        branch = (
            100.0 if branches == 0 else 100.0 * summary["covered_branches"] / branches
        )
        print(f"{lines:6.1f} {branch:6.1f} {name}")
        if lines < FLOOR:
            problems.append(f"{name}: lines {lines:.1f}% < {FLOOR}%")
        if branch < FLOOR:
            problems.append(f"{name}: branches {branch:.1f}% < {FLOOR}%")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
