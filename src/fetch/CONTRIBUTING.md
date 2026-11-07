# Developing Fetch

Run these commands from `src/fetch`. Python environments and dependencies use uv;
the lockfile supplies the exact package versions. Node.js is optional at runtime.

```sh
uv sync --locked
uv run --locked python scripts/provision_node_deps.py
uv run --locked python ci/check.py lint
uv run --locked python ci/check.py test --no-node
uv run --locked python ci/check.py test --node
uv run --locked python ci/check.py coverage
uv run --locked python ci/check.py package
```

Tests use mocked HTTP or loopback fixtures. Selected capabilities must be available;
skipped tests fail the suite. Coverage requires 90% lines and branches per source file.
Packaging checks inspect source and wheel archives and launch the installed server
against both supported MCP protocol eras. `ci/build-constraints.txt` pins the build backend.

The repository's `.github/workflows/fetch-ci.yml` defines the platform matrix.
The Docker check requires a running Linux container engine and runs on Linux CI.
Windows checks refuse to launch Docker; ordinary Windows testing needs no Linux VM.

User installation and client configuration are documented in [README.md](README.md).
