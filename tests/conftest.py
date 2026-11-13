"""Expose the shared fixtures in harness.py to pytest, and set suite-wide policy.

The fixtures live in harness.py beside the helpers the tests import from it;
pytest only discovers fixtures and hooks from conftest.py, so this file
re-exports them and holds the collection hook and the Hypothesis profile.
"""

import os
import sys
import tempfile
from types import ModuleType
from typing import Any

import pytest
from hypothesis import settings
from hypothesis.configuration import set_hypothesis_home_dir

from .harness import (
    network_guard,
    node_readability,
    private_tempdir,
    python_readability,
    web,
    worker_in_process,
)

__all__ = [
    "network_guard",
    "node_readability",
    "private_tempdir",
    "python_readability",
    "web",
    "worker_in_process",
]

# Hypothesis keeps its example database and caches out of the repository root.
set_hypothesis_home_dir(
    os.path.join(tempfile.gettempdir(), "mcp-server-fetch-hypothesis")
)
settings.register_profile("dev", database=None)
settings.register_profile("ci", database=None, derandomize=True)
settings.load_profile("ci" if os.environ.get("CI") else "dev")


@pytest.fixture
def posix_resource(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Exercise pure POSIX limit logic on any host without changing real limits."""
    resource = ModuleType("resource")
    for name, value in {
        "RLIM_INFINITY": -1,
        "RLIMIT_CPU": 0,
        "RLIMIT_CORE": 1,
        "RLIMIT_DATA": 2,
        "getrlimit": lambda kind: (-1, -1),
        "setrlimit": lambda kind, limits: None,
    }.items():
        setattr(resource, name, value)
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sys, "platform", "linux")
    return resource


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Every test that needs the Readability.js capability carries the `node` marker,
    # so the Node-absent lane can deselect exactly those (-m "not node").
    for item in items:
        if "node_readability" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.node)
