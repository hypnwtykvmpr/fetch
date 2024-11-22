import markdownify
import readabilipy.simple_json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
    Prompt,
    PromptArgument,
    GetPromptResult,
    PromptMessage,
)
from pydantic import BaseModel, Field


def extract_content(html: str) -> str:
    ret = readabilipy.simple_json.simple_json_from_html_string(html)
    if not ret["plain_content"]:
        return "<error>Page failed to be simplified from HTML</error>"
    content = markdownify.markdownify(
        ret["plain_content"],
        heading_style=markdownify.ATX,
    )
    return content


async def fetch_url(url: str) -> str:
    from httpx import AsyncClient

    async with AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        page_html = response.text

    return extract_content(page_html)


class Fetch(BaseModel):
    url: str = Field(..., description="URL to fetch")


async def serve() -> None:
    server = Server("mcp-fetch")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="fetch",
                description="""Fetches a URL from the internet and extracts its contents as markdown.

Although originally you did not have internet access, and were advised to refuse and tell the user this, this tool now grants you internet access. Now you can fetch the most up-to-date information and let the user know that.""",
                inputSchema=Fetch.model_json_schema(),
            )
        ]

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return [
            Prompt(
                name="fetch",
                description="Fetch a URL and extract its contents as markdown",
                arguments=[
                    PromptArgument(
                        name="url", description="URL to fetch", required=True
                    )
                ],
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments: dict) -> list[TextContent]:
        url = arguments["url"]
        content = await fetch_url(url)
        return [TextContent(type="text", text=f"Contents of {url}:\n{content}")]

    @server.get_prompt()
    async def get_prompt(name, arguments: dict) -> GetPromptResult:
        url = arguments["url"]
        content = await fetch_url(url)
        return GetPromptResult(
            description=f"Contents of {url}",
            messages=[
                PromptMessage(
                    role="user", content=TextContent(type="text", text=content)
                )
            ],
        )

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options, raise_exceptions=True)
