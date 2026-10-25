"""Time-bound an awaitable without waiting for it to finish cancelling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def _consume(task: asyncio.Task) -> None:
    # Retrieve the exception so an abandoned task never logs "exception was never retrieved".
    if not task.cancelled():
        task.exception()


def _abandon(task: asyncio.Task) -> None:
    task.add_done_callback(_consume)
    task.cancel()


async def run_bounded(awaitable: Awaitable[T], timeout: float) -> T:
    """Result of `awaitable`, or TimeoutError after `timeout` seconds.

    Unlike asyncio.wait_for, a timeout does not await the cancelled work: a driver that is slow to
    honour cancellation (asyncpg's pool release on a blackholed connection) cannot stretch the bound.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        _abandon(task)
        raise
    if not done:
        _abandon(task)
        raise TimeoutError
    return task.result()
