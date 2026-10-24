"""CancelGeneration and later caller speech must be read while a turn is blocked on the LLM or a
tool, not only between the turn's yielded responses."""
import asyncio
from unittest.mock import AsyncMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer
from services.conversation.session import HandlerResponse


def _open():
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        routing_status=pb.ROUTING_STATUS_ROUTED,
    ))


def _speech_ended():
    return pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=500))


def _cancel():
    return pb.GatewayMessage(cancel_generation=pb.CancelGeneration(session_id="s1", trace_id="tr"))


def _servicer(on_speech_ended):
    handler = AsyncMock()
    handler.out_responses = None
    handler.on_speech_ended = on_speech_ended

    async def factory(ctx):
        return handler

    return ConversationServicer(factory), handler


async def _run(servicer, messages, until, timeout=1.0):
    """Send `messages`, collect ServiceMessage payload names until `until(names)` or timeout."""
    async def stream():
        for msg in messages:
            yield msg
        await asyncio.Event().wait()   # gateway keeps the stream open

    names: list[str] = []
    done = asyncio.Event()

    async def consume():
        async for out in servicer.Converse(stream(), AsyncMock()):
            names.append(out.WhichOneof("payload"))
            if until(names):
                done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return names


async def test_cancel_is_acked_while_turn_is_blocked_on_llm():
    async def blocked_turn(session_id, audio, duration_ms, energy_db):
        await asyncio.Event().wait()   # LLM / tool never answers
        yield HandlerResponse()

    servicer, handler = _servicer(blocked_turn)
    names = await _run(servicer, [_open(), _speech_ended(), _cancel()], lambda n: "cancel_ack" in n)

    assert "cancel_ack" in names
    handler.on_cancel.assert_awaited_once()
    handler.on_session_end.assert_awaited_once()


async def test_speech_during_blocked_turn_is_handled_after_it_in_order():
    turns = []
    release = asyncio.Event()

    async def turn(session_id, audio, duration_ms, energy_db):
        turns.append(len(turns))
        if len(turns) == 1:
            await release.wait()
        yield HandlerResponse(stt_text=f"turn{len(turns)}")

    servicer, _ = _servicer(turn)

    async def stream():
        yield _open()
        yield _speech_ended()
        await asyncio.sleep(0.05)
        yield _speech_ended()   # arrives while turn 1 is still blocked
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.Event().wait()

    texts: list[str] = []
    done = asyncio.Event()

    async def consume():
        async for out in servicer.Converse(stream(), AsyncMock()):
            if out.WhichOneof("payload") == "stt_result" and out.stt_result.text:
                texts.append(out.stt_result.text)
                if len(texts) == 2:
                    done.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(done.wait(), 2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert texts == ["turn1", "turn2"]
