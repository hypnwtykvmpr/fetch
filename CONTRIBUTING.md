# Developing Fetch

Use this repository's root `pyproject.toml` and `uv.lock`. Python dependencies
and development commands are managed by uv.

```sh
uv sync --locked --dev
uv run --locked python scripts/provision_node_deps.py
uv run --locked python ci/check.py lint
uv run --locked python ci/check.py test --no-node
uv run --locked python ci/check.py test --node
uv run --locked python ci/check.py coverage
uv run --locked python ci/check.py package
```

Node provisioning is needed only for Node-enabled checks. Tests use mocked HTTP
or loopback fixtures. Selected capabilities must be present; skips fail the suite.
Coverage requires 90% lines and branches per source file. Packaging checks inspect
archives and exercise both supported MCP protocol eras.

Run checks affected by a change. The [Fetch workflow](.github/workflows/fetch-ci.yml)
defines the platform matrix. Installation is documented in [README.md](README.md).
