"""A call handled entirely inside a call flow still gets a `calls` row (#176)."""
import asyncio
import os
import uuid
from unittest.mock import MagicMock

from libs.config_sdk.callflow import parse_graph
from libs.config_sdk.dial_targets import TransferPolicy

from ..callflow.handler import CallFlowConversationHandler
from ..callflow.runner import CallFlowRunner
from ..transcript_builder import TranscriptBuilder
from .test_callflow_handler import (
    FakeTTS, _collect_to_agent_graph, _delegate_factory, _dial_graph, _no_handoff, _no_voice,
)

os.environ.setdefault("POSTGRES_DSN", "postgresql://satish@localhost:5432/voiceai")

_ROW = dict(direction="inbound", caller_number="+15550001", called_number="+15550002")


def _handler(graph_raw: dict, session_id: str, transcripts, handoff=_no_handoff) -> CallFlowConversationHandler:
    return CallFlowConversationHandler(
        CallFlowRunner(parse_graph(graph_raw), tts_config_id=None),
        tts=FakeTTS(), sample_rate=8000, session_id=session_id, tenant_id="default", call_id="c1",
        handoff=handoff, voice_for=_no_voice, agent_slugs={"agent-uuid-1": "support-bot"},
        transfer_policy=TransferPolicy(), transcripts=transcripts, call_row=_ROW,
    )


async def test_a_dial_only_call_gets_a_row_that_is_closed_at_session_end():
    builder = await TranscriptBuilder.connect(os.environ["POSTGRES_DSN"])
    session_id = f"test-flow-row-{uuid.uuid4().hex[:8]}"
    handler = _handler(_dial_graph(), session_id, builder)
    try:
        await asyncio.sleep(0.05)   # the dial node runs; no agent is ever reached
        await handler.on_session_end(session_id, "stream_ended")
        await builder._chains[session_id]

        row = await builder._pool.fetchrow(
            "SELECT direction, caller_number, called_number, ended_at, close_reason "
            "FROM calls WHERE session_id = $1", session_id,
        )
        assert row is not None
        assert (row["direction"], row["caller_number"], row["called_number"]) == (
            "inbound", "+15550001", "+15550002",
        )
        assert row["ended_at"] is not None and row["close_reason"] == "stream_ended"
    finally:
        await builder._pool.execute("DELETE FROM calls WHERE session_id = $1", session_id)
        await builder.close()


async def test_the_row_is_begun_before_the_flow_runs_and_ended_once_without_a_delegate():
    transcripts = MagicMock()
    handler = _handler(_dial_graph(), "s1", transcripts)

    transcripts.begin_call.assert_called_once_with("s1", "default", "c1", **_ROW)
    await asyncio.sleep(0.05)
    await handler.on_session_end("s1", "stream_ended", final_state="TRANSFER_SUCCESS")

    transcripts.end_call.assert_called_once_with(
        "s1", "stream_ended", final_state="TRANSFER_SUCCESS", score_sentiment=False,
    )


async def test_after_a_handoff_the_delegate_alone_ends_the_call():
    transcripts = MagicMock()
    handler = _handler(
        _collect_to_agent_graph(sensitive=False), "s1", transcripts, handoff=_delegate_factory({}),
    )
    await asyncio.sleep(0)
    for digit in "1234":
        await handler.on_dtmf("s1", digit)
    await asyncio.sleep(0.05)

    await handler.on_session_end("s1", "stream_ended")

    transcripts.end_call.assert_not_called()   # the delegate (no transcripts of its own here) owns it
