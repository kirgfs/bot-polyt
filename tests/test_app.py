from __future__ import annotations

import asyncio

from polybot.recorder.app import supervise


async def forever() -> None:
    await asyncio.sleep(3600)


async def test_stop_signal_is_clean() -> None:
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    assert await supervise({"a": forever, "b": forever}, stop) is True


async def test_crashing_component_is_not_clean() -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    assert await supervise({"a": forever, "boom": boom}, asyncio.Event()) is False


async def test_component_that_requests_stop_is_clean() -> None:
    stop = asyncio.Event()

    async def guard() -> None:  # like GeoGuard: sets stop, then returns
        stop.set()

    assert await supervise({"a": forever, "guard": guard}, stop) is True
