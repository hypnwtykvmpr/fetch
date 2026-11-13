"""A green suite contains no skipped tests, including collection-time skips."""

import pytest

_skips = 0


def pytest_sessionstart() -> None:
    global _skips
    _skips = 0


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _skips
    _skips += int(report.skipped)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    global _skips
    _skips += int(report.skipped)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _skips:
        if session.exitstatus in (0, 1, 5):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", f"zero-skip policy: {_skips} skipped report(s)")
