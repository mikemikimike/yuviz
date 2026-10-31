import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.conversation import pipeline as pipeline_module
from services.conversation.directives import TransferRequest, TransferType
from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer, _consume_pending_transfer, _is_reply_audio
from services.conversation.session import HandlerResponse

from .test_end_call_guard import _FAREWELL
from .test_pipeline import _make_handler, _make_llm, _make_stt, _scripted_tts, _silence


def _make_request(trigger: str) -> TransferRequest:
    return TransferRequest(
        session_id="s1", tenant_id="t1", call_id="c1",
        transfer_type=TransferType.WARM, destination="1000",
        reason="some reason", trigger=trigger,
    )


def test_llm_directive_transfer_dropped_on_barge_in():
    """The caller's own request — if they still want a human, they'll
    just ask again, so a barge-in silently dropping it is fine."""
    tr = _make_request("llm_directive")
    assert _consume_pending_transfer(tr, interrupted=True, sid="s1") is None


def test_escalation_transfer_survives_barge_in():
    """A system-initiated escalation transfer isn't cancelled by caller speech (barge-in)."""
    tr = _make_request("escalation_threshold")
    out = _consume_pending_transfer(tr, interrupted=True, sid="s1")
    assert out is not None
    assert out.transfer_request.destination == "1000"


def test_llm_directive_transfer_sent_when_not_interrupted():
    tr = _make_request("llm_directive")
    out = _consume_pending_transfer(tr, interrupted=False, sid="s1")
    assert out is not None
    assert out.transfer_request.destination == "1000"


def test_transfer_with_empty_destination_is_refused_regardless_of_trigger():
    tr = _make_request("escalation_threshold")
    tr = TransferRequest(
        session_id="s1", tenant_id="t1", call_id="c1",
        transfer_type=TransferType.WARM, destination="",
        reason="some reason", trigger="escalation_threshold",
    )
    assert _consume_pending_transfer(tr, interrupted=False, sid="s1") is None


def test_reply_audio_requires_a_non_empty_payload():
    assert _is_reply_audio(SimpleNamespace(filler=False, tts_payloads=[b"\x01\x02"]))
    assert not _is_reply_audio(SimpleNamespace(filler=False, tts_payloads=[b""]))
    assert not _is_reply_audio(SimpleNamespace(filler=False, tts_payloads=[]))
    assert not _is_reply_audio(SimpleNamespace(filler=True, tts_payloads=[b"\x01\x02"]))


@pytest.mark.asyncio
async def test_only_the_max_duration_cap_sends_an_end_call_when_no_audio_played(caplog):
    handler = _make_handler(
        _make_stt("okay thanks"), _make_llm(["[[END_CALL]]"]), _scripted_tts(default="none"),
        system_prompt="You are Alex.", farewell_message=_FAREWELL,
    )
    handler._session("s1").first_turn_filler_spoken = True
    real_turn = handler.on_speech_ended
    turns = iter([None])

    async def on_speech_ended(session_id, audio, duration_ms, energy_db):
        if next(turns, "real") is None:
            yield HandlerResponse(
                stt_text="okay thanks", end_call=True, end_call_without_audio=True,
                end_call_grace_period_ms=700,
            )
            return
        async for response in real_turn(session_id, audio, duration_ms, energy_db):
            yield response

    handler.on_speech_ended = on_speech_ended

    async def factory(ctx):
        return handler

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        for _ in range(2):
            yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
            yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
            await asyncio.sleep(0.2)
        await asyncio.Event().wait()

    messages = []

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            messages.append(msg)

    with caplog.at_level("WARNING", logger=pipeline_module.log.name):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(consume(), 1)

    end_calls = [m.end_call for m in messages if m.HasField("end_call")]
    assert [(e.reason, e.grace_period_ms) for e in end_calls] == [("max_duration_no_audio", 700)]
    kinds = [m.WhichOneof("payload") for m in messages]
    assert kinds[kinds.index("end_call") - 1] == "tts_chunk"
    assert "End of call withheld" in caplog.text


@pytest.mark.asyncio
async def test_end_call_is_withheld_when_the_only_tts_payload_is_empty():
    handler = _make_handler(
        _make_stt("okay thanks"), _make_llm(["Bye."]), _scripted_tts(default="none"),
        system_prompt="You are Alex.", farewell_message=_FAREWELL,
    )

    async def on_speech_ended(session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(stt_text="okay thanks", tts_payloads=[b""], end_call=True)

    handler.on_speech_ended = on_speech_ended

    async def factory(ctx):
        return handler

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.sleep(0.3)
        await asyncio.Event().wait()

    messages = []

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            messages.append(msg)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(consume(), 1)

    assert any(m.HasField("stt_result") for m in messages)
    assert not any(m.HasField("end_call") for m in messages)


@pytest.mark.asyncio
async def test_an_empty_tts_payload_sends_no_early_tts_started_or_chunk():
    handler = _make_handler(
        _make_stt("okay thanks"), _make_llm(["Bye."]), _scripted_tts(default="none"),
        system_prompt="You are Alex.", farewell_message=_FAREWELL,
    )

    async def on_speech_ended(session_id, audio, duration_ms, energy_db):
        yield HandlerResponse(stt_text="okay thanks", tts_payloads=[b""])

    handler.on_speech_ended = on_speech_ended

    async def factory(ctx):
        return handler

    async def stream():
        yield pb.GatewayMessage(session_open=pb.SessionOpenRequest(
            protocol_version="1.0", session_id="s1", tenant_id="t1",
            routing_status=pb.ROUTING_STATUS_ROUTED,
        ))
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        await asyncio.sleep(0.3)
        await asyncio.Event().wait()

    messages = []

    async def consume():
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            messages.append(msg)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(consume(), 1)

    # The turn is still closed once (by the no-audio fallback), after STT and with no audio chunk.
    kinds = [m.WhichOneof("payload") for m in messages]
    assert kinds.count("tts_started") == 1
    assert kinds.index("stt_result") < kinds.index("tts_started")
    assert not any(m.tts_chunk.payload for m in messages if m.HasField("tts_chunk"))
    assert not any(m.HasField("tts_chunk") and not m.tts_chunk.is_final for m in messages)
