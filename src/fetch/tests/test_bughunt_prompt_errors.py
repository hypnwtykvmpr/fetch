"""Bug-hunt repros: error propagation on the fetch prompt path."""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from .harness import FakeWeb, connect

PAGE = "https://example.com/page"


def wire(result) -> dict:
    return result.model_dump(by_alias=True, mode="json", exclude_none=True)


async def test_prompt_url_httpx_rejects_is_invalid_params(web: FakeWeb) -> None:
    # Malformed arguments are protocol errors; valid URLs that fail to fetch
    # still return prompt text. Reject malformed IPv6 before any HTTP request.
    async with connect() as (session, _):
        with pytest.raises(MCPError) as excinfo:
            await session.get_prompt("fetch", {"url": "http://[::1"})
    assert excinfo.value.error.code == INVALID_PARAMS
    assert "Invalid URL 'http://[::1'" in excinfo.value.error.message
    assert "invalid IPv6 address" in excinfo.value.error.message
    assert web.requests == []
