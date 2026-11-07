"""Regression checks for the shared qualification runner."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import check
import docker_context_check
import pytest
import yaml


@pytest.mark.parametrize("in_actions", [False, True])
def test_nodefree_path_does_not_resync_the_active_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, in_actions: bool
) -> None:
    # Matrix rows may use a different Python than .python-version. Filtering
    # PATH must not ask uv to replace the environment of its running caller.
    monkeypatch.setattr(check, "UV", str(tmp_path / "must-not-launch-uv"))
    github_env = tmp_path / "github-env"
    github_env.write_text("UNCHANGED=value\n", encoding="utf-8")
    if in_actions:
        monkeypatch.setenv("GITHUB_ENV", str(github_env))
    else:
        monkeypatch.delenv("GITHUB_ENV", raising=False)
    original = os.environ.get("PATH", "")
    result = check._nodefree_env()
    assert os.environ.get("PATH", "") == original
    assert result["PATH"], "PATH filtering must return stdout even inside Actions"
    assert github_env.read_text(encoding="utf-8") == "UNCHANGED=value\n"
    assert all(
        shutil.which(tool, path=result["PATH"]) is None
        for tool in ("node", "npm", "corepack")
    )


@pytest.mark.parametrize("entry", ["stage", "context"])
def test_windows_docker_checks_never_launch_a_process(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("Windows Docker check attempted an external process")

    monkeypatch.setattr(check.sys, "platform", "win32")
    monkeypatch.setattr(check, "run", unexpected)
    monkeypatch.setattr(check.subprocess, "check_output", unexpected)
    monkeypatch.setattr(check.subprocess, "run", unexpected)
    with pytest.raises(SystemExit, match="WSL"):
        if entry == "stage":
            check.docker_stage()
        else:
            docker_context_check.docker("info")


def test_run_can_feed_a_check_script_via_stdin() -> None:
    check.run(
        [
            sys.executable,
            "-c",
            "import sys; assert sys.stdin.buffer.read() == b'fetch-script'",
        ],
        stdin=b"fetch-script",
    )


@pytest.mark.parametrize("job_name", ["test", "package"])
def test_windows_workflow_jobs_keep_the_bash_shell(job_name: str) -> None:
    workflow = yaml.safe_load(
        (check.ROOT.parents[1] / ".github/workflows/fetch-ci.yml").read_text(
            encoding="utf-8"
        )
    )
    job = workflow["jobs"][job_name]
    assert "windows-latest" in job["strategy"]["matrix"]["os"]
    # A job's defaults.run replaces the workflow mapping; its working directory
    # must not silently discard the shell required by the inline scripts.
    defaults = job.get("defaults", workflow["defaults"])["run"]
    run_steps = [step for step in job["steps"] if "run" in step]
    assert run_steps
    for step in run_steps:
        assert step.get("shell", defaults.get("shell")) == "bash", (
            f"{job_name}: {step.get('name', step['run'])} selects the Windows shell"
        )


@pytest.mark.parametrize("global_autocrlf", ["true", "false"])
def test_docker_stage_archives_the_package_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, global_autocrlf: str
) -> None:
    # Hooks may export repository paths; keep every Git object in this fixture.
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    ):
        monkeypatch.delenv(variable, raising=False)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        f"[core]\n\tautocrlf = {global_autocrlf}\n", encoding="ascii"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    git = shutil.which("git")
    assert git is not None, "the qualification runner requires Git"
    repo = tmp_path / "repository"
    package = repo / "src/fetch"
    package.mkdir(parents=True)
    git_dir = repo / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/fixture\n", encoding="ascii")
    (git_dir / "config").write_text(
        "[core]\n\tautocrlf = false\n\teol = lf\n", encoding="ascii"
    )

    def git_object(command: str, data: bytes, *args: str) -> str:
        return (
            subprocess.check_output([git, command, *args], input=data, cwd=repo)
            .decode("ascii")
            .strip()
        )

    files = {
        "Dockerfile": b"FROM scratch\n",
        ".dockerignore": b"*\n!src/\n",
    }
    blobs = {
        name: git_object("hash-object", content, "-w", "--stdin")
        for name, content in files.items()
    }
    package_tree = git_object(
        "mktree",
        "".join(f"100644 blob {oid}\t{name}\n" for name, oid in blobs.items()).encode(),
    )
    src_tree = git_object("mktree", f"040000 tree {package_tree}\tfetch\n".encode())
    root_tree = git_object("mktree", f"040000 tree {src_tree}\tsrc\n".encode())
    # Git archive accepts a tree-valued HEAD; no index, commit or hook is needed.
    (git_dir / "HEAD").write_text(f"{root_tree}\n", encoding="ascii")

    class ContextChecked(Exception):
        pass

    def inspect_context(argv: list[str], **kwargs: object) -> None:
        assert argv[:-1] == [
            check.UV,
            "run",
            "--locked",
            "python",
            "ci/docker_context_check.py",
        ], f"unexpected external command: {argv}"
        context = Path(argv[-1])
        for name, content in files.items():
            assert (context / name).is_file(), f"archive omitted {name}"
            assert (context / name).read_bytes() == content
        # Stop before any Docker invocation, on every host platform.
        raise ContextChecked

    monkeypatch.setattr(check, "ROOT", package)
    monkeypatch.setattr(check, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(check, "run", inspect_context)
    with pytest.raises(ContextChecked):
        check.docker_stage()


def _assert_source_packages_match_workspaces(repo: Path) -> None:
    root_manifest = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    workspaces = {
        manifest.parent.relative_to(repo).as_posix()
        for pattern in root_manifest["workspaces"]
        for manifest in repo.glob(f"{pattern}/package.json")
    }
    assert workspaces, "the root manifest must declare server workspaces"
    # Upstream discovers packages before dependency installation. Model that
    # clean checkout even when this test runs after uv or npm creates packages.
    discovered = {
        manifest.parent.relative_to(repo).as_posix()
        for manifest in (repo / "src").rglob("package.json")
        if not {".venv", "node_modules"}.intersection(manifest.relative_to(repo).parts)
    }
    assert discovered == workspaces, (
        f"unexpected package discovery: {sorted(discovered - workspaces)}; "
        f"missing server workspaces: {sorted(workspaces - discovered)}"
    )


def test_recursive_typescript_discovery_only_finds_server_workspaces() -> None:
    _assert_source_packages_match_workspaces(check.ROOT.parents[1])


@pytest.mark.parametrize(
    "site_packages", ["Lib/site-packages", "lib/python3.15/site-packages"]
)
def test_source_discovery_ignores_generated_environments_but_rejects_nested_sources(
    tmp_path: Path, site_packages: str
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"workspaces": ["src/*"]}), encoding="utf-8"
    )
    for name in ("first-server", "second-server"):
        manifest = tmp_path / "src" / name / "package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
    for directory in (
        f"src/fetch/.venv/{site_packages}/pyright/dist",
        f"src/fetch/.venv/{site_packages}/readabilipy/javascript",
        "src/first-server/node_modules/dependency",
    ):
        manifest = tmp_path / directory / "package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
    _assert_source_packages_match_workspaces(tmp_path)

    nested_source = tmp_path / "src/fetch/scripts/readability-js/package.json"
    nested_source.parent.mkdir(parents=True)
    nested_source.write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError, match="src/fetch/scripts/readability-js"):
        _assert_source_packages_match_workspaces(tmp_path)
