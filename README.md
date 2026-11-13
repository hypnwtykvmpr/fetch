# Fetch MCP Server

This standalone fork of the [upstream Fetch server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)
returns web pages as text or Markdown through one MCP `fetch` tool and one prompt.
Its additions protect the fetching agent against hostile page content and
unbounded network or extraction work. PII passes through unchanged; it is neither
detected nor redacted. The reported fork version is `2026.10.28+fork.1`.

## Installation

Clone this repository and run these commands in its root, beside `pyproject.toml`:

```sh
uv sync --locked --no-dev
uv run --locked --no-dev mcp-server-fetch --help
```

Python 3.11 or newer is required; this checkout selects Python 3.15.
uv manages the locked project environment on Windows, macOS, and Linux.
After updating the checkout, run `uv sync --locked --no-dev` again.

The MCP client launches the stdio server. Starting it without `--help` waits
for MCP input; it does not start an HTTP listener.

### Optional Readability.js extraction

Python extraction works without Node.js. If supported Node.js is already on the
client's `PATH`, provision the optional extractor through this project:

```sh
uv run --locked --no-dev python scripts/provision_node_deps.py
```

Supported Node versions are `^22.22.2 || ^24.15.0 || >=26.0.0`; CI uses Node 24.
Provisioning uses the committed dependency lock and never runs during a fetch.
An unavailable or failed Node capability probe selects Python extraction.
`--no-readability-js` always selects Python.

### Supported platforms

| Platform | Architecture | Python | Node.js extractor |
|---|---|---|---|
| Linux (`ubuntu-latest`) | x86_64 | 3.11, 3.11.4, 3.12, 3.13, 3.14, 3.15 | with and without Node: all listed |
| Windows (`windows-latest`) | x64 | 3.11, 3.15 | with and without Node: all listed |
| macOS (`macos-latest`) | arm64 | 3.11, 3.15 | with and without Node: all listed |

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

Replace `<checkout-directory>` with this clone's absolute path. If the client
cannot find uv on `PATH`, set `command` to its installed executable.

```json
{
  "mcpServers": {
    "fetch": {
      "command": "uv",
      "args": ["run", "--locked", "--no-dev", "--directory", "<checkout-directory>", "mcp-server-fetch"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Keep each argument in its own array entry, including paths containing spaces.
Restart the client after configuration changes or extractor provisioning.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for project-scoped development commands.
The workflow tests this server with and without its optional Node extractor.

## Upstream history

Only the upstream `src/fetch` history is retained. The fork is one commit above
that extracted baseline; [UPSTREAM.json](UPSTREAM.json) identifies its source.
The fork retains normalized robots-path checks, public-address checks, bounded
decompression and workers, and the agent-protection rules described above.

## License

MIT. See [LICENSE](LICENSE).
