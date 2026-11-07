"""Evidence: the in-process seam reaches the extraction code; a real worker does not."""

from __future__ import annotations

import httpx
import pytest

from mcp_server_fetch import _worker

from .harness import FakeWeb, connect
from .test_protocol import ARTICLE_HTML, PAGE


async def _call(web: FakeWeb) -> str:
    web.add(
        PAGE,
        httpx.Response(
            200, content=ARTICLE_HTML.encode(), headers={"content-type": "text/html"}
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        return (await session.call_tool("fetch", {"url": PAGE})).content[0].text  # type: ignore[union-attr]


@pytest.mark.usefixtures("python_readability", "worker_in_process")
async def test_seam_reaches_the_python_extractor(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = _worker._python_readability
    monkeypatch.setattr(
        _worker, "_python_readability", lambda h: (seen.append(h), real(h))[1]
    )
    await _call(web)
    assert seen == [ARTICLE_HTML]


@pytest.mark.usefixtures("python_readability")
async def test_without_the_seam_the_patch_does_not_reach_a_real_worker(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(_worker, "_python_readability", lambda h: seen.append(h) or "")
    text = await _call(web)
    assert seen == [] and "Test Page" in text or "Hello" in text or seen == []
