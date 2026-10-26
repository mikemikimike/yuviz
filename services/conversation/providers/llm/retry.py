"""
RetryOnceLLM — retries an ILLM once on failure, only if nothing was yielded yet.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from typing import Any, AsyncGenerator, Callable, Iterator

from ..interfaces import ChatMessage

log = logging.getLogger(__name__)
_STREAM_CLEANUP_TIMEOUT_S = 0.1


class LlmResponseTimeout(TimeoutError):
    """The response deadline expired before any output reached the caller."""


class _ResponseBudget:
    """Share one first-output deadline and completion signal across retry attempts."""

    def __init__(self, deadline: float, output_started: asyncio.Event) -> None:
        self.deadline = deadline
        self.output_started = output_started


_response_budget: ContextVar[_ResponseBudget | None] = ContextVar("llm_response_budget", default=None)


@contextmanager
def response_budget(timeout_s: float, output_started: asyncio.Event) -> Iterator[None]:
    """Share one first-output deadline across LLM attempts in a turn."""
    token = _response_budget.set(_ResponseBudget(asyncio.get_running_loop().time() + timeout_s, output_started))
    try:
        yield
    finally:
        _response_budget.reset(token)


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


async def _next_with_budget(read_task: asyncio.Task[Any], budget: _ResponseBudget) -> Any:
    """Wait for one item until the shared deadline or until caller output starts."""
    remaining = budget.deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        read_task.cancel()
        raise LlmResponseTimeout("LLM response deadline expired")

    output_task = asyncio.create_task(budget.output_started.wait())
    try:
        done, _ = await asyncio.wait(
            {read_task, output_task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        output_task.cancel()
        if not read_task.done():
            read_task.cancel()
        raise

    output_task.cancel()
    output_task.add_done_callback(_consume_task)
    if read_task in done:
        return read_task.result()
    if output_task in done:
        return await read_task

    read_task.cancel()
    raise LlmResponseTimeout("LLM response deadline expired")


class RetryOnceLLM:
    """Retry provider failures before the stream emits its first item."""

    def __init__(self, llm: Any, *, name: str) -> None:
        self._llm = llm
        self._name = name

    async def _with_retry(self, make_call: Callable[[], AsyncGenerator[Any, None]]) -> AsyncGenerator[Any, None]:
        """Apply one pre-output retry and the active turn's first-output budget."""
        yielded_any = False
        budget = _response_budget.get()

        async def stream_call() -> AsyncGenerator[Any, None]:
            nonlocal yielded_any
            stream = make_call()
            iterator = stream.__aiter__()
            completed = False
            pending_read: asyncio.Task[Any] | None = None
            try:
                while True:
                    try:
                        if budget is None or budget.output_started.is_set():
                            item = await iterator.__anext__()
                        else:
                            pending_read = asyncio.create_task(iterator.__anext__())
                            try:
                                item = await _next_with_budget(pending_read, budget)
                            finally:
                                if pending_read.done():
                                    pending_read = None
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
