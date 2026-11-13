"""Cancelled conversions must retain admission until their worker thread stops.

An asyncio future can be cancelled while its thread keeps processing. These
tests pin the processor cap across native asyncio and AnyIO cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import anyio
import httpx
import pytest

from mcp_server_fetch import server

from .harness import FakeWeb


@pytest.mark.parametrize("cancel_kind", ["asyncio", "anyio"])
@pytest.mark.parametrize("pipeline_fails", [False, True])
@pytest.mark.parametrize("stage", ["text", "node-probe"])
async def test_cancelled_text_conversion_keeps_its_slot_until_thread_exit(
    monkeypatch: pytest.MonkeyPatch,
    web: FakeWeb,
    cancel_kind: str,
    pipeline_fails: bool,
    stage: str,
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    thread_finished = asyncio.Event()
    caller_finished = asyncio.Event()
    release = threading.Event()
    scopes: list[anyio.CancelScope] = []
    helper_state = server._fetch_call_state()
    slots = helper_state.slots if stage == "node-probe" else server._new_slots()

    def blocked_pipeline(*args):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(5):
                raise TimeoutError("test did not release the conversion thread")
            if pipeline_fails:
                raise ValueError("conversion failed after cancellation")
            return None if stage == "node-probe" else ("converted", {}, "")
        finally:
            loop.call_soon_threadsafe(thread_finished.set)

    if stage == "node-probe":
        monkeypatch.setattr(server._worker, "find_node", blocked_pipeline)
        web.add(
            "https://example.com/",
            httpx.Response(
                200,
                text="<html><body>article</body></html>",
                headers={"content-type": "text/html"},
            ),
        )
    else:
        monkeypatch.setattr(server, "_text_pipeline", blocked_pipeline)

    async def render():
        try:
            if stage == "node-probe":
                await server.fetch_url("https://example.com/", "Legacy/1.0")
                return
            await server._render(
                "body",
                "text/plain",
                page_url="https://example.com/",
                raw=False,
                sanitize=True,
                include_links=False,
                node=None,
                slots=slots,
                deadline=None,
            )
        finally:
            caller_finished.set()

    async def scoped_render():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await render()

    task = asyncio.create_task(scoped_render() if cancel_kind == "anyio" else render())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel_kind == "anyio":
            scopes[0].cancel()
        else:
            task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert slots.running._value == server._MAX_WORKERS - 1
        assert not caller_finished.is_set()
        if cancel_kind == "asyncio":
            # Repeated native cancellation must not interrupt the drain either.
            task.cancel()
            await asyncio.sleep(0)
            assert slots.running._value == server._MAX_WORKERS - 1
        release.set()
        if cancel_kind == "asyncio":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert thread_finished.is_set()
        assert caller_finished.is_set()
        assert slots.running._value == server._MAX_WORKERS
        assert slots.admitted._value == server._MAX_WORKERS + server._MAX_WAITERS
    finally:
        release.set()
        await asyncio.wait_for(thread_finished.wait(), timeout=2)
        with contextlib.suppress(asyncio.CancelledError):
            await task
