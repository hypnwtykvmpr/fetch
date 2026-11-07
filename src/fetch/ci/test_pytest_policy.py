"""Exercise the zero-skip rule through pytest's real exit status."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def test_ok(): assert True", 0),
        ("import pytest\ndef test_skip(): pytest.skip('missing capability')", 1),
        (
            "import pytest\n@pytest.mark.skipif(True, reason='platform')\ndef test_skip(): pass",
            1,
        ),
        ("import pytest\npytest.skip('module', allow_module_level=True)", 1),
    ],
)
def test_skips_make_pytest_red(tmp_path: Path, source: str, expected: int) -> None:
    policy = Path(__file__).resolve().parents[1] / "conftest.py"
    if policy.exists():
        shutil.copyfile(policy, tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_default_fixture_loop_scope = function\n", encoding="utf-8"
    )
    (tmp_path / "test_case.py").write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
