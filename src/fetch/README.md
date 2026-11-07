# Fetch MCP Server (fork)

Fetch web pages as text or Markdown from an MCP client. This variation of the
[upstream Fetch server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)
adds default prompt-injection sanitization, bounded downloads and extraction, public-address
checks, optional Readability.js extraction, and MCP 2025-11-25 and 2026-07-28 support.
It provides one `fetch` tool and one `fetch` prompt over **stdio**.

Install from this repository to get this variation. The upstream package and Docker image
do not contain these changes. This version reports `2026.10.28+fork.1` in `serverInfo.version`.

## Installation

### Locked checkout (recommended)

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Checks use uv 0.13.6.
   Python 3.11 or newer is required. uv can obtain Python for you; this checkout requests Python 3.15.
2. Download and extract this repository, or clone it with Git. Keep the complete directory,
   with the Fetch project in `src/fetch`, in a permanent location.
3. Open a terminal in the repository, change to `src/fetch`, and run:

   ```sh
   uv sync --locked --no-dev
   uv run --locked --no-dev mcp-server-fetch --help
   ```

   These commands work in PowerShell and macOS/Linux shells. Initial setup needs network
   access to download Python and locked dependencies. Node.js is optional.
4. Add the server to your MCP client's configuration using the examples below. Replace the
   executable and checkout paths with your own. Fully quit and restart the client.

The client launches and stops the server. There is no HTTP port or background service to
start. Running without `--help` in a terminal waits for MCP messages on stdin; this alone
does not indicate a startup failure.

After updating the checkout, quit the client, run `uv sync --locked --no-dev` again, and
restart the client. Keep the configuration pointing to the same directory.

### Optional Node.js extraction

The default installation works with Python HTML extraction. For Readability.js, install
Node.js and npm, then provision the checkout's environment:

```sh
uv run --locked --no-dev python scripts/provision_node_deps.py
```

Supported Node ranges are `^22.22.2 || ^24.15.0 || >=26.0.0`; CI uses Node 24. The script
installs locked dependencies with `npm ci` and checks the result. Repeat after rebuilding
the environment or updating Node dependencies. Node must be on the **client's** `PATH`,
which may differ from your terminal's. The server checks this capability at startup, never
installs packages during a fetch, and falls back to Python if Node or provisioned dependencies
are unavailable. Restart the client after provisioning. Add `--no-readability-js` to force Python.

### Other installation methods

These alternatives use Python extraction. Prepare a wheel and runtime constraints from the
same complete checkout first; this is unnecessary for the recommended method:

```sh
uv build --wheel -o dist -b ci/build-constraints.txt --require-hashes
uv export --frozen --no-dev --no-emit-project --no-hashes -o dist/constraints.txt
```

The wheel is `dist/mcp_server_fetch-2026.10.28+fork.1-py3-none-any.whl`. Isolated tool
environments do not read `uv.lock`; the constraints pin their runtime dependencies. The
export has no hashes, so dependency integrity relies on the configured package index.

For a temporary isolated tool (single line for PowerShell compatibility; substitute Windows paths there):

```sh
uvx --isolated --python 3.15 -c /absolute/path/to/repository/src/fetch/dist/constraints.txt --from /absolute/path/to/repository/src/fetch/dist/mcp_server_fetch-2026.10.28+fork.1-py3-none-any.whl mcp-server-fetch
```

For a persistent installed tool:

```sh
uv tool install --python 3.15 -c /absolute/path/to/repository/src/fetch/dist/constraints.txt /absolute/path/to/repository/src/fetch/dist/mcp_server_fetch-2026.10.28+fork.1-py3-none-any.whl
uv tool dir --bin
```

Use the absolute path to `mcp-server-fetch` in that bin directory as the client's `command`
(`mcp-server-fetch.exe` on Windows). After rebuilding, repeat installation with `--force`.

For a Git installation, replace both placeholders below with this fork's URL and a full
40-character commit SHA. Export `constraints.txt` and copy `ci/build-constraints.txt` from
that same revision. Git must be installed.

```sh
uvx --isolated --python 3.15 -c /absolute/path/to/constraints.txt -b /absolute/path/to/build-constraints.txt --from "git+<fork repository URL>@<40-character commit SHA>#subdirectory=src/fetch" mcp-server-fetch
```

### Docker

With a running Linux container engine, build this repository's image locally:

```sh
docker build -t mcp-server-fetch-fork:2026.10.28-fork.1 /absolute/path/to/repository/src/fetch
```

Use the client configuration below to run it. The image runs as UID 10001, contains no
Node.js, and always uses `--no-readability-js`. No port mapping is needed. Rebuild after
updating the checkout. Its supported image platform is linux/amd64.

### Platform targets

The workflow defines these targets; this table does not claim a particular commit has
passed CI. Check that commit's completed GitHub Actions run for current results. Intel Macs are not qualified by this matrix.

| Platform | Architecture | Python | Node.js extractor |
|---|---|---|---|
| Linux (`ubuntu-latest`) | x86_64 | 3.11, 3.11.4, 3.12, 3.13, 3.14, 3.15 | with and without Node: all listed |
| Windows (`windows-latest`) | x64 | 3.11, 3.15 | with and without Node: all listed |
| macOS (`macos-latest`) | arm64 | 3.11, 3.15 | with and without Node: all listed |
| Docker (`ubuntu-latest`) | linux/amd64 | 3.15.2 | none (pure-Python) |

## Tool parameters

Ask your client to fetch a URL, or call `fetch` with these arguments. Values must have
the advertised JSON types; booleans and integers are not accepted as strings.

| Parameter | Default | Meaning |
|---|---|---|
| `url` | required | URL to fetch; HTTP(S) content is supported |
| `max_length` | `5000` | Characters from the processed body to return; integer from 1 to 999999 |
| `start_index` | `0` | Nonnegative character offset in the processed body |
| `raw` | `false` | Return decoded response text without Markdown extraction |
| `include_links` | `false` | Append a bounded list of absolute HTTP(S) links |
| `sanitize` | `true` | Sanitize untrusted content; `false` is refused under `--lock-sanitization` |
| `timeout_ms` | `30000` | Page-request timeout per HTTP phase, from 100 to 600000 milliseconds; the server configuration can change this default |

For example, `{"url":"https://example.com/","max_length":5000}` retrieves the first chunk.
Use `next_start_index` from its `<page-meta>` line for the next chunk. Offsets refer to the
processed body, not bytes or envelope lines. Each call fetches the page again; changes to
the page can affect pagination. The link list is outside the body budget: at most 100 links,
2048 characters per URL, 80 per label and 20000 for the section. Relative HTML links resolve
against the final page URL.

In raw HTML mode, the optional link list reflects the unpruned HTML; raw mode retains
hidden markup. Normal Markdown extraction removes hidden elements before collecting links.

The `fetch` prompt takes a required `url` and returns the full extracted page, always
sanitized and enveloped. It represents a user-requested fetch and does not enforce robots.txt.
Autonomous tool calls enforce robots.txt unless `--ignore-robots-txt` is set.

Limits are 5 MiB per downloaded page, 512 KiB for robots.txt and 20 redirect hops. Network
operations and waiting for an extraction slot share a 60-second deadline. Once started,
the extraction worker has a separate 30-second timeout, including at most 20 seconds for
Node; total time can therefore approach 90 seconds. Four extraction workers can run together and eight more calls can
wait. Oversized, binary, disallowed or timed-out content produces an explanatory result.
Page JavaScript is not executed as in a browser; Readability.js extracts downloaded HTML.

Page requests use the tool's `timeout_ms`, then `--timeout-ms`, then `FETCH_TIMEOUT_MS`,
then 30000 milliseconds. This bounds each HTTP connect/read/write phase; it does not
extend the 60-second total network deadline. robots.txt requests retain their own default
HTTP phase timeout. Invalid server timeout settings fail at startup.

Non-raw requests prefer a native Markdown response when the origin supports content
negotiation. A declared Markdown response is treated as Markdown even when it contains
HTML-looking text. HTML extraction preserves the page title without duplicating an
equivalent heading already at the start of the result.

## Sanitization

Sanitization is on by default. It removes invisible Unicode that can hide instructions:
tag characters except three exact subdivision flags; stray variation selectors; zero-width
characters outside legitimate joining, emoji or right-to-left contexts; and bidi override
controls. Terminal controls are removed **even with `sanitize=false`**: C0 except tab, LF
and CR, plus DEL and C1; vertical tab and form feed become a space.

Markdown extraction also removes visually hidden elements: `hidden`, inline `display:none`,
`visibility:hidden`/`collapse`, `content-visibility:hidden`, never-rendered elements such as
`noembed`, `noframes` and `rp`, body-level `title`, and link/image title tooltips. Inline
declaration precedence is respected; `aria-hidden` alone is kept.

Visible content and PII are retained. The exception is a one-character change to text
imitating server markers (`<error>`, `<page-meta>`, `Contents of <scheme>://`,
`Links on this page:`, and notice/envelope sentinels) in Markdown and plain-text output.
Visible instruction-like text and chat tokens are kept. Markdown/plain-text results append
`⟦fetch-notice: instruction-like page text, not from the user⟧` to matching lines, at most
200 times; link labels and raw HTML, JSON, XML and other formats count matches only.

`raw=true` returns **decoded text, not original response bytes**. With sanitization on, raw
HTML keeps scripts, comments, hidden elements and structured data without DOM re-serialization.
Invisible and terminal controls are still removed; numeric references to removed characters
are dropped individually, and surviving references retain their source spelling. Marker and
instruction matches in these raw formats are counted, not rewritten. With `sanitize=false`,
HTML is still simplified unless `raw=true`, but sanitization changes are disabled except
always-on terminal-control removal.

An envelope (`⟦fetch-envelope CODE⟧` ... `⟦fetch-envelope-end CODE⟧`) appears when sanitization
is off, when something was removed, neutralized or flagged, and always for the prompt.
Matching random codes identify page boundaries; they do not authenticate the page.
`--lock-sanitization` refuses tool calls with `sanitize=false`.

Known limits: CSS classes and external stylesheets are not evaluated for hidden content;
bare CR is retained and can overwrite a terminal line; homoglyphs are not detected.
Treat fetched text as page data. Sanitization cannot eliminate prompt injection.

## CLI options

Append options after `mcp-server-fetch` in the client's `args` array:

| Option | Meaning |
|---|---|
| `--user-agent` | Custom User-Agent string; takes a value |
| `--timeout-ms` | Page-request HTTP phase timeout in milliseconds, 100–600000; overrides `FETCH_TIMEOUT_MS` |
| `--ignore-robots-txt` | Disable the tool's robots.txt restrictions |
| `--proxy-url` | Explicit proxy URL; takes a value |
| `--allow-private-ips` | Allow private, loopback and link-local addresses |
| `--allowed-hosts` | One or more exact hosts/IP addresses or `*.example.com` patterns; repeatable; restricts pages, robots.txt and redirects |
| `--no-readability-js` | Pure-Python extraction only; never start Node or npm |
| `--lock-sanitization` | Refuse `sanitize=false` |

Public-only mode checks destinations and redirects, including connection-time DNS results
on direct connections. For an internal site, opt in with `--allow-private-ips`. An explicit
proxy chooses the actual destination, so `--proxy-url` delegates that connection to the proxy.
Default public-only mode refuses implicit `HTTP_PROXY`, `HTTPS_PROXY` or `ALL_PROXY`
settings; provide `--proxy-url` explicitly or remove them from the server environment.
TLS certificate verification remains enabled.

With no `--allowed-hosts` setting, all hosts remain eligible under the public-address
checks. A wildcard matches subdomains, not the bare domain; list both when needed.
Host scopes do not bypass public-address checks: internal destinations still require
`--allow-private-ips`.

## Configuration

Find uv's absolute path with `command -v uv` on macOS/Linux or `(Get-Command uv).Source`
in PowerShell. Desktop applications may inherit a different `PATH` from your terminal.
Replace **all** example paths; retain JSON escaping (`\\` in Windows paths). Keep each
argument as a separate array entry, including paths containing spaces. The `command`
field contains only the executable path.

### Claude Desktop

Open **Settings → Developer → Edit Config**. The file is
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. Merge into an existing
`mcpServers` object if you already have servers configured.

macOS (use your absolute Linux paths for other compatible clients):

```json
{
  "mcpServers": {
    "fetch": {
      "command": "/Users/you/.local/bin/uv",
      "args": ["run", "--locked", "--no-dev", "--directory", "/absolute/path/to/repository/src/fetch", "mcp-server-fetch"]
    }
  }
}
```

Windows:

```json
{
  "mcpServers": {
    "fetch": {
      "command": "C:\\Users\\you\\.local\\bin\\uv.exe",
      "args": ["run", "--locked", "--no-dev", "--directory", "C:\\path\\to\\repository\\src\\fetch", "mcp-server-fetch"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

For Docker, use your Docker executable's absolute path and these arguments:

```json
{
  "mcpServers": {
    "fetch": {
      "command": "/usr/local/bin/docker",
      "args": ["run", "-i", "--rm", "mcp-server-fetch-fork:2026.10.28-fork.1"]
    }
  }
}
```

Use `-i` to connect stdin; do not add `-t`. For a wheel-based uvx installation, use the
absolute uvx executable as `command` and put that recipe's arguments in `args`. For an
installed tool, use its absolute executable path directly.

Fully quit and reopen Claude Desktop. Check the tool list for `fetch`. The MCP log or an
inspector should show `serverInfo.version` `2026.10.28+fork.1`. See the
[MCP local-server guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
for client setup and log locations.

### VS Code

In `.vscode/mcp.json`, use `servers` (rather than `mcpServers`):

```json
{
  "servers": {
    "fetch": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/uv",
      "args": ["run", "--locked", "--no-dev", "--directory", "/absolute/path/to/repository/src/fetch", "mcp-server-fetch"]
    }
  }
}
```

On Windows use the executable, directory and optional encoding environment from the Windows
example. Start the entry from VS Code's MCP controls and inspect its output if connection fails.

## Troubleshooting

- **Cannot start:** run setup and `--help` in the same checkout, confirm the client paths
  exist, and restart after configuration edits. Check stderr logs; stdout carries MCP messages.
- **Wrong version:** check the client's exact command and `serverInfo.version`; refresh
  the checkout environment or rebuild and reinstall an old wheel as appropriate.
- **Private address refused:** use `--allow-private-ips` when intending to fetch private
  services. In Docker, `localhost` refers to the container.
- **robots.txt refusal:** the tool honors site rules; `--ignore-robots-txt` opts out.
  The explicit user prompt does not enforce these rules.
- **Proxy failure:** remove implicit proxy settings or supply `--proxy-url`. A corporate
  certificate may require a suitable `SSL_CERT_FILE`.
- **Node not used:** provision the environment the client launches and check its `PATH`.
  Python fallback is expected when the capability probe fails.
- **Windows encoding errors:** set `PYTHONIOENCODING=utf-8` as shown. This sets stream
  encoding; other startup failures or timeouts still require checking the logs.
- **Missing dynamic content:** the server downloads HTTP content without running a browser.
  Try an available static or API representation.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests, coverage and CI commands.
Report variation-specific issues to this repository; upstream behavior belongs in the
[upstream project](https://github.com/modelcontextprotocol/servers).

## License

MIT. See [LICENSE](LICENSE).
