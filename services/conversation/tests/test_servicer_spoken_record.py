"""What the stored transcript calls spoken is what the servicer actually sent the caller, and a hang-up
during a transfer-failed apology is persisted by teardown, before the session is ended."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.servicer import ConversationServicer

from .test_pipeline import _make_handler, _make_llm, _make_stt, _make_tts, _silence


def _open() -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        routing_status=pb.ROUTING_STATUS_ROUTED,
    ))


def _recording_transcripts() -> tuple[MagicMock, asyncio.Event]:
    transcripts = MagicMock()
    recorded = asyncio.Event()
    transcripts.record_turn.side_effect = lambda *args, **kwargs: recorded.set()
    return transcripts, recorded


async def test_a_barge_in_drained_during_synthesis_stores_only_the_sentence_that_was_sent(monkeypatch):
    monkeypatch.setenv("VOICEAI_FILLER_DELAY_MS", "500")
    transcripts, recorded = _recording_transcripts()
    tts = _make_tts()
    first_audio = tts.synthesize.return_value
    second_started, release_second = asyncio.Event(), asyncio.Event()

    async def synthesize(text, sample_rate):
        if "two" in text:
            second_started.set()
            await release_second.wait()
        return first_audio

    tts.synthesize = synthesize
    handler = _make_handler(
        _make_stt("hello"), _make_llm(["Sentence one. ", "Sentence two."]), tts, transcripts=transcripts,
    )
    gateway: asyncio.Queue = asyncio.Queue()

    async def stream():
        yield _open()
        yield pb.GatewayMessage(audio_chunk=pb.AudioChunk(session_id="s1", payload=_silence()))
        yield pb.GatewayMessage(speech_ended=pb.SpeechEndedNotification(session_id="s1", duration_ms=1500))
        yield await gateway.get()

    async def factory(ctx):
        return handler

    async def consume():
        sent_one = False
        async for msg in ConversationServicer(factory).Converse(stream(), AsyncMock()):
            if msg.HasField("tts_chunk") and not sent_one:
                sent_one = True
                await second_started.wait()
                # The barge-in lands while sentence two is still being synthesized.
                gateway.put_nowait(pb.GatewayMessage(
                    cancel_generation=pb.CancelGeneration(session_id="s1", trace_id="tr")))
                await asyncio.sleep(0.05)
                release_second.set()
            if recorded.is_set():
                return

    await asyncio.wait_for(consume(), 2)

    args = transcripts.record_turn.call_args.args
    assert args[3] == "Sentence one."
    assert args[4] is True


async def test_a_hangup_during_the_transfer_failed_apology_is_persisted_before_the_session_ends():
    transcripts, recorded = _recording_transcripts()
    handler = _make_handler(_make_stt(), _make_llm([]), _make_tts(), transcripts=transcripts)

    async def stream():
        yield _open()
        yield pb.GatewayMessage(transfer_failed=pb.TransferFailed(
            session_id="s1", destination="+15551234567", reason="esl_unreachable", transfer_id="x1"))
        await asyncio.Event().wait()   # the gateway holds the stream open until it hangs up

    async def factory(ctx):
        return handler

    converse = ConversationServicer(factory).Converse(stream(), AsyncMock())
    async for msg in converse:
        if msg.HasField("tts_chunk"):
            await converse.aclose()   # hung up while the servicer waits at the apology's yield
            break
    await asyncio.sleep(0.05)   # a finalizer that outlived teardown would have run by now

    assert recorded.is_set()
    writes = [c[0] for c in transcripts.method_calls if c[0] in ("record_turn", "end_call")]
    assert writes == ["record_turn", "end_call"]
    assert transcripts.record_turn.call_args.args[1] == "[transfer_failed: esl_unreachable]"
    assert "s1" not in handler._sessions
