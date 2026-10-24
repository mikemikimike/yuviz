"""A turn is persisted even when the caller hangs up mid-turn (#172)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer

from .test_pipeline import _make_handler, _make_stt, _make_tts, _silence


def _stalling_llm(spoken: str) -> MagicMock:
    async def _gen(messages):
        yield spoken
        await asyncio.Event().wait()   # the rest of the reply never arrives

    llm = MagicMock()
    llm.generate = _gen
    return llm


async def test_hangup_mid_reply_persists_the_turn_as_interrupted():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("book me in"), _stalling_llm("Sure thing."), _make_tts(), transcripts=transcripts,
    )

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.Event().wait()   # the gateway holds the stream open until it hangs up

    first_audio = asyncio.Event()

    async def factory(ctx):
        return handler

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            if msg.HasField("tts_chunk"):
                first_audio.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_audio.wait(), 2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[:2] == ("s1", "book me in")
    assert args[3] == "Sure thing."
    assert args[4] is True


async def test_hangup_while_waiting_on_the_model_persists_the_caller_utterance():
    transcripts = MagicMock()

    async def never(messages):
        await asyncio.Event().wait()
        yield ""   # pragma: no cover

    llm = MagicMock()
    llm.generate = never
    handler = _make_handler(_make_stt("pay my order"), llm, _make_tts(), transcripts=transcripts)

    async def turn():
        async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
            pass

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    transcripts.record_turn.assert_called_once()
    args = transcripts.record_turn.call_args.args
    assert args[1] == "pay my order"
    assert args[4] is True
