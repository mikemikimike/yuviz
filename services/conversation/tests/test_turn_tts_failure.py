"""A TTS failure must not leave the caller in silence or be stored as spoken text (#174)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer

from .test_pipeline import _make_handler, _make_llm, _make_stt, _silence


def _tts_failing_on(marker: str) -> MagicMock:
    tts = MagicMock()

    async def _stream(text, sample_rate):
        if marker in text:
            raise RuntimeError("tts down")
        yield b"\x00" * 320

    tts.synthesize_stream = _stream
    return tts


async def test_a_sentence_that_failed_tts_is_not_stored_as_spoken():
    transcripts = MagicMock()
    handler = _make_handler(
        _make_stt("when is it"),
        _make_llm(["Your booking QATTSFAIL is done.", " Anything else?"]),
        _tts_failing_on("QATTSFAIL"), transcripts=transcripts,
    )

    async for _ in handler.on_speech_ended("s1", _silence(), 200, -20.0):
        pass

    assert transcripts.record_turn.call_args.args[3] == "Anything else?"
    assert handler._get_history("s1")[-1].content == "Anything else?"


async def test_a_reply_whose_tts_failed_entirely_is_not_added_to_history(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "60000")
    handler = _make_handler(
        _make_stt("when is it"), _make_llm(["Your booking QATTSFAIL is done."]),
        _tts_failing_on("QATTSFAIL"),
    )

    responses = [r async for r in handler.on_speech_ended("s1", _silence(), 200, -20.0)]

    assert not any(r.tts_payloads or r.response_text for r in responses)
    assert [m.role for m in handler._get_history("s1")] == ["system"]


async def test_a_turn_with_no_audio_still_sends_the_terminal_chunk(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "60000")
    handler = _make_handler(
        _make_stt("when is it"), _make_llm(["Your booking QATTSFAIL is done."]),
        _tts_failing_on("QATTSFAIL"),
    )

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.Event().wait()

    async def factory(ctx):
        return handler

    kinds = []

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            kinds.append(msg.WhichOneof("payload"))
            if msg.HasField("tts_chunk"):
                assert msg.tts_chunk.is_final and not msg.tts_chunk.payload
                return

    await asyncio.wait_for(consume(), 2)

    assert kinds[-3:] == ["stt_result", "tts_started", "tts_chunk"]
