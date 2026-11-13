from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import serve as serve


def __getattr__(name: str) -> Any:
    # Lazy, so `python -I -m mcp_server_fetch._worker` (one per fetch) does not load the MCP SDK.
    if name == "serve":
        from .server import serve

        return serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main():
    """MCP Fetch Server - HTTP fetching functionality for MCP"""
    import argparse
    import asyncio
    import os

    from . import server
    from .allowlist import parse_allowed_host
    from .timeout import (
        DEFAULT_TIMEOUT_MS,
        MAX_TIMEOUT_MS,
        MIN_TIMEOUT_MS,
        TIMEOUT_ENV,
        parse_timeout_ms,
        timeout_ms_from_env,
    )

    parser = argparse.ArgumentParser(
        description="give a model the ability to make web requests"
    )
    parser.add_argument("--user-agent", type=str, help="Custom User-Agent string")
    parser.add_argument(
        "--timeout-ms",
        type=parse_timeout_ms,
        help=f"HTTP request timeout in milliseconds, {MIN_TIMEOUT_MS} to "
        f"{MAX_TIMEOUT_MS} (default: ${TIMEOUT_ENV}, else {DEFAULT_TIMEOUT_MS}); "
        "a tool call's timeout_ms argument overrides it",
    )
    parser.add_argument(
        "--ignore-robots-txt",
        action="store_true",
        help="Ignore robots.txt restrictions",
    )
    parser.add_argument("--proxy-url", type=str, help="Proxy URL to use for requests")
    parser.add_argument(
        "--allow-private-ips",
        action="store_true",
        help="Allow fetching private, loopback and link-local addresses (refused by default)",
    )
    parser.add_argument(
        "--allowed-hosts",
        nargs="+",
        action="extend",
        type=parse_allowed_host,
        metavar="HOST",
        help="Only fetch from these hosts: an exact host name or IP address, or "
        "*.example.com for any subdomain of example.com (all hosts by default)",
    )
    parser.add_argument(
        "--no-readability-js",
        action="store_true",
        help="Extract HTML with the pure-Python extractor only, never starting Node.js "
        "or npm (by default Readability.js is used when Node.js is on PATH)",
    )

    parser.add_argument(
        "--lock-sanitization",
        action="store_true",
        help="Refuse fetch calls with sanitize=false, so fetched pages are always sanitized",
    )

    args = parser.parse_args()
    if args.timeout_ms is not None:
        os.environ[TIMEOUT_ENV] = str(args.timeout_ms)
    try:
        timeout_ms_from_env()
    except ValueError as e:
        parser.error(str(e))
    # Looked up at call time, so a test may patch mcp_server_fetch.serve or mcp_server_fetch.server.serve.
    serve = globals().get("serve", server.serve)
    asyncio.run(
        serve(
            custom_user_agent=args.user_agent,
            ignore_robots_txt=args.ignore_robots_txt,
            proxy_url=args.proxy_url,
            allow_private_ips=args.allow_private_ips,
            **({"allowed_hosts": args.allowed_hosts} if args.allowed_hosts else {}),
            use_readability_js=not args.no_readability_js,
            lock_sanitization=args.lock_sanitization,
        )
    )


if __name__ == "__main__":
    main()
