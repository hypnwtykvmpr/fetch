from .server import serve


def main():
    """MCP Fetch Server - HTTP fetching functionality for MCP"""
    import asyncio

    asyncio.run(serve())


if __name__ == "__main__":
    main()
