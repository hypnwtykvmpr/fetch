"""Allowlist check for built archives, and canary planting for the leak checks.

uv run --locked python ci/check_dist.py plant <dir>      plant synthetic private files (refuses existing paths)
uv run --locked python ci/check_dist.py inspect <dist>   check the one sdist and the one wheel in <dist>
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

CANARY = b"FETCH-PRIVACY-CANARY"
# Representative private configuration and generated files must stay outside archives.
# These paths are outside the reviewed shipping manifest, or carry an extension Hatchling excludes.
CANARY_PATHS = (
    ".audit/CANARY.txt",
    ".private/CANARY.txt",
    ".config/CANARY.json",
    ".cache/CANARY",
    ".state/CANARY",
    ".notes/CANARY.md",
    ".credentials/CANARY.key",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "QWEN.md",
    ".env",
    ".env.local",
    ".envrc",
    "secrets/CANARY.key",
    "CANARY.pem",
    "CANARY.p12",
    ".venv/CANARY.txt",
    ".hypothesis/CANARY",
    ".pytest_cache/CANARY",
    ".ruff_cache/CANARY",
    ".coverage",
    "coverage.json",
    "dist/CANARY.txt",
    "build/CANARY.txt",
    "src/mcp_server_fetch/.env",
    "src/mcp_server_fetch/CANARY.key",
    "src/mcp_server_fetch/CANARY.pem",
    "src/mcp_server_fetch/__pycache__/CANARY.cpython-315.pyc",
    "tests/.CANARY",
    "tests/__pycache__/CANARY.pyc",
)
# Runtime modules allowed in release archives.
SRC_MODULES = (
    "title.py",
    "timeout.py",
    "readability_js.py",
    "allowlist.py",
    "__init__.py",
    "__main__.py",
    "_emoji_ranges.py",
    "_textsan.py",
    "_worker.py",
    "server.py",
)
# Regression tests allowed in source archives.
TEST_MODULES = (
    "test_text_pipeline_cancellation.py",
    "test_upstream_api_parity.py",
    "test_timeout.py",
    "test_readability_subprocess.py",
    "test_prompt_url.py",
    "test_page_title.py",
    "test_markdown_negotiation.py",
    "test_allowed_hosts.py",
    "__init__.py",
    "conftest.py",
    "corpus_injection.py",
    "corpus_parser.py",
    "corpus_unicode.py",
    "harness.py",
    "test_emoji_ranges.py",
    "test_envelope.py",
    "test_fail_closed.py",
    "test_links.py",
    "test_pii_passthrough.py",
    "test_sanitize_budgets.py",
    "test_sanitize_corpus.py",
    "test_sanitize_dom.py",
    "test_sanitize_markers.py",
    "test_sanitize_switch.py",
    "test_sanitize_unicode.py",
    "test_seam_reaches_extraction.py",
    "test_worker.py",
    "test_worker_branches.py",
    "oracle_textsan.py",
    "test_bughunt_convert_props.py",
    "test_bughunt_deep_html.py",
    "test_bughunt_header_charset.py",
    "test_bughunt_legacy_charset.py",
    "test_bughunt_pagination_props.py",
    "test_bughunt_prompt_errors.py",
    "test_bughunt_robots.py",
    "test_capabilities.py",
    "test_empty_tree.py",
    "test_entrypoints.py",
    "test_eras.py",
    "test_hermetic.py",
    "test_hidden_ssr.py",
    "test_html_charset.py",
    "test_missing_content_type.py",
    "test_non_cp1252_pages.py",
    "test_props_decode.py",
    "test_props_fetch_semantics.py",
    "test_props_url_and_robots.py",
    "test_protocol.py",
    "test_public_only_backend.py",
    "test_raw_prefix_and_deps.py",
    "test_readability_js_opt_out.py",
    "test_robots_status.py",
    "test_security_guard_fail_open.py",
    "test_security_readabilipy_subprocess.py",
    "test_security_response_limits.py",
    "test_security_url_handling.py",
    "test_server.py",
    "test_translated_addresses.py",
    "test_transport_streaming.py",
)
SDIST_TOP_LEVEL = {
    "PKG-INFO",
    "README.md",
    "CONTRIBUTING.md",
    "conftest.py",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
}
WHEEL_DISTINFO = re.compile(
    r"mcp_server_fetch-[^/]+\.dist-info/(METADATA|WHEEL|RECORD|entry_points\.txt|licenses/LICENSE)"
)
# Runtime modules required in wheels.
WHEEL_REQUIRED = tuple(f"mcp_server_fetch/{name}" for name in SRC_MODULES)


def sdist_allowed(rest: str) -> bool:
    if rest in SDIST_TOP_LEVEL:
        return True
    if rest.startswith("src/mcp_server_fetch/"):
        return rest.removeprefix("src/mcp_server_fetch/") in SRC_MODULES
    if rest.startswith("tests/"):
        return rest.removeprefix("tests/") in TEST_MODULES
    return False


def wheel_allowed(name: str) -> bool:
    if name.startswith("mcp_server_fetch/"):
        return name.removeprefix("mcp_server_fetch/") in SRC_MODULES
    return WHEEL_DISTINFO.fullmatch(name) is not None


def plant(root: str) -> None:
    base = Path(root)
    for rel in CANARY_PATHS:
        path = base / rel
        if path.exists():
            sys.exit(f"refusing to overwrite existing path: {path}")
    for rel in CANARY_PATHS:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CANARY + b"\n")


def inspect(dist: str) -> int:
    d = Path(dist)
    sdists, wheels = sorted(d.glob("*.tar.gz")), sorted(d.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        print(f"need exactly one sdist and one wheel in {d}: {sdists} {wheels}")
        return 1
    problems: list[str] = []
    with tarfile.open(sdists[0]) as tf:
        members = tf.getmembers()
        n_sdist = len(members)
        for m in members:
            if m.issym() or m.islnk():
                problems.append(f"sdist link member: {m.name}")
                continue
            if m.isdir():
                continue
            rest = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not sdist_allowed(rest):
                problems.append(f"sdist unexpected member: {m.name}")
            f = tf.extractfile(m)
            if f is not None and CANARY in f.read():
                problems.append(f"sdist canary bytes in: {m.name}")
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        n_wheel = len(names)
        for name in names:
            if name.endswith("/"):
                continue
            if not wheel_allowed(name):
                problems.append(f"wheel unexpected member: {name}")
            if CANARY in zf.read(name):
                problems.append(f"wheel canary bytes in: {name}")
        for req in WHEEL_REQUIRED:
            if req not in names:
                problems.append(f"wheel missing required member: {req}")
        for name in names:
            if name.endswith(".dist-info/METADATA"):
                meta = zf.read(name).decode("utf-8")
                lines = meta.splitlines()
                if "License-Expression: MIT" not in lines:
                    problems.append("METADATA lacks 'License-Expression: MIT'")
                versions = [ln for ln in lines if ln.startswith("Version:")]
                if not any(
                    re.fullmatch(r"Version: \d+\.\d+\.\d+\+fork\.\d+", ln)
                    for ln in versions
                ):
                    problems.append(f"METADATA version is not N.N.N+fork.N: {versions}")
                if any(ln.startswith("Requires-Dist: requests") for ln in lines):
                    problems.append("METADATA still requires requests")
    print(f"sdist {n_sdist} members, wheel {n_wheel} members")
    for p in problems:
        print(p)
    return 1 if problems else 0


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"plant", "inspect"}:
        print(__doc__, file=sys.stderr)
        return 2
    if sys.argv[1] == "plant":
        plant(sys.argv[2])
        return 0
    return inspect(sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
