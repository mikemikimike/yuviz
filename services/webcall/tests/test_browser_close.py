"""A browser that closes its WebSocket normally must end the Conversation call, or the session
(and its live STT stream) leaks until the process exits."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from services.webcall import __main__ as webcall
from services.webcall.tests.test_first_frame import CRED_FRAME, PLAIN_PATH, FakeCall, FakeWs, pb


class OpenUntilHalfClosedCall(FakeCall):
    """Like the real Conversation stream: the reply stream stays open until the client half-closes."""

    def __init__(self) -> None:
        super().__init__()
        self.write_side_closed = asyncio.Event()

    async def done_writing(self) -> None:
        self.write_side_closed.set()

    async def _replies(self):
        session_open = self.session_opens()[0]
        yield pb.ServiceMessage(service_ready=pb.ServiceReady(session_id=session_open.session_id))
        await self.write_side_closed.wait()


async def test_normal_browser_close_half_closes_call_so_session_ends(monkeypatch):
    call = OpenUntilHalfClosedCall()

    class Channel:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(webcall.grpc.aio, "insecure_channel", lambda target: Channel())
    monkeypatch.setattr(
        webcall.pb_grpc, "ConversationServiceStub", lambda channel: SimpleNamespace(Converse=lambda: call),
    )
    ws = FakeWs(PLAIN_PATH, [CRED_FRAME, json.dumps({"type": "speech_ended", "duration_ms": 100})])

    await asyncio.wait_for(webcall._handle_connection(ws), 2)

    assert call.write_side_closed.is_set()
