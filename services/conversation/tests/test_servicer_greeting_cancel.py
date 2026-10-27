"""A caller who hangs up while the greeting is still synthesizing must still get the
session closed (call record finalized), not leaked."""
import asyncio
from unittest.mock import AsyncMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer


async def _open_stream():
    yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        routing_status=pb.ROUTING_STATUS_ROUTED,
    ))
    await asyncio.Event().wait()   # gateway keeps the stream open


async def test_cancel_during_greeting_still_closes_session():
    greeting_started = asyncio.Event()
    handler = AsyncMock()
    handler.out_responses = None

    async def slow_greeting(session_id):
        greeting_started.set()
        await asyncio.Event().wait()   # TTS queued behind other calls
    handler.greeting.side_effect = slow_greeting

    async def factory(ctx):
        return handler

    async def consume():
        async for _ in ConversationServicer(factory).Converse(_open_stream(), AsyncMock()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(greeting_started.wait(), 1)
    task.cancel()                       # gateway cancels the RPC on hangup
    try:
        await task
    except asyncio.CancelledError:
        pass

    handler.on_session_end.assert_awaited_once()
    assert handler.on_session_end.await_args.args[1] == "stream_ended"


async def test_wait_closed_waits_for_cancelled_streams_to_close_their_session():
    """Shutdown cancels streams; their end-of-call write is only queued once teardown ran, so the
    caller needs to wait for it before closing the transcript pool."""
    greeting_started = asyncio.Event()
    handler = AsyncMock()
    handler.out_responses = None

    async def slow_greeting(session_id):
        greeting_started.set()
        await asyncio.Event().wait()
    handler.greeting.side_effect = slow_greeting

    ended = []

    async def slow_session_end(*args, **kwargs):
        await asyncio.sleep(0.05)   # teardown still running when the cancelling caller moves on
        ended.append(args[1])
    handler.on_session_end.side_effect = slow_session_end

    async def factory(ctx):
        return handler

    servicer = ConversationServicer(factory)

    async def consume():
        async for _ in servicer.Converse(_open_stream(), AsyncMock()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(greeting_started.wait(), 1)
    task.cancel()
    await servicer.wait_closed(timeout=1)

    assert ended == ["stream_ended"]
