"""
RetryOnceLLM — retries an ILLM once on failure, only if nothing was yielded yet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable

from ..interfaces import ChatMessage

log = logging.getLogger(__name__)
_STREAM_CLEANUP_TIMEOUT_S = 0.1


class LlmResponseTimeout(TimeoutError):
    """The response deadline expired before any output reached the caller."""


def _consume_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a background task's exception to avoid an unobserved-task warning."""
    if not task.cancelled():
        task.exception()


def _schedule_stream_close(stream: Any) -> None:
    """Close a provider stream asynchronously with a bounded wait."""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return

    async def close_bounded() -> None:
        close_task = asyncio.create_task(aclose())
        done, _ = await asyncio.wait({close_task}, timeout=_STREAM_CLEANUP_TIMEOUT_S)
        if not done:
            close_task.cancel()
            close_task.add_done_callback(_consume_task)
        else:
            _consume_task(close_task)

    cleanup_task = asyncio.create_task(close_bounded())
    cleanup_task.add_done_callback(_consume_task)


class RetryOnceLLM:
    """Retry provider failures before the stream emits its first item."""

    def __init__(self, llm: Any, *, name: str) -> None:
        self._llm = llm
        self._name = name

    async def _with_retry(self, make_call: Callable[[], AsyncGenerator[Any, None]]) -> AsyncGenerator[Any, None]:
        """Apply one pre-output retry and the active turn's first-output budget."""
        yielded_any = False
        async def stream_call() -> AsyncGenerator[Any, None]:
            nonlocal yielded_any
            stream = make_call()
            iterator = stream.__aiter__()
            completed = False
            pending_read: asyncio.Task[Any] | None = None
            try:
                while True:
                    try:
                        item = await iterator.__anext__()
                    except StopAsyncIteration:
                        completed = True
                        return
                    yielded_any = True
                    yield item
            finally:
                if not completed:
                    if pending_read is not None and not pending_read.done():
                        if not pending_read.cancelling():
                            pending_read.cancel()

                        def close_after_read(task: asyncio.Task[Any]) -> None:
                            _consume_task(task)
                            _schedule_stream_close(stream)

                        pending_read.add_done_callback(close_after_read)
                    else:
                        _schedule_stream_close(stream)

        try:
            async for item in stream_call():
                yield item
            return
        except LlmResponseTimeout:
            raise
        except Exception:
            if yielded_any:
                raise
            log.warning("RetryOnceLLM: %s failed before yielding output — retrying once", self._name, exc_info=True)
        async for item in stream_call():
            yield item

    async def generate(self, messages: list[ChatMessage]) -> AsyncGenerator[str, None]:
        async for token in self._with_retry(lambda: self._llm.generate(messages)):
            yield token

    async def generate_with_tools(
        self, messages: list[ChatMessage], schemas: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        async for event in self._with_retry(
            lambda: self._llm.generate_with_tools(messages, schemas, tool_choice=tool_choice)
        ):
            yield event

    async def warm(self) -> None:
        warm = getattr(self._llm, "warm", None)
        if warm is not None:
            await warm()

    async def aclose(self) -> None:
        aclose = getattr(self._llm, "aclose", None)
        if aclose is not None:
            await aclose()
