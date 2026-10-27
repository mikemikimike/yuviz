"""Servicer handling of a refused test session (guided agent creation, T12)."""
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from services.conversation.generated.voiceai.v1 import conversation_pb2 as pb
from services.conversation.rejection import RejectedCallHangup
from services.conversation.servicer import ConversationServicer
from services.conversation.session import AgentUnavailable
from services.conversation.tools import policy_resolver
from services.conversation.tools.policy_resolver import ToolPolicyResolver
from services.conversation.tools.registry import ToolRegistry

SENTINEL = "sentinel-credential-7f3a9c"


async def _stream(*messages):
    for m in messages:
        yield m


def _open(credential: str = SENTINEL) -> pb.GatewayMessage:
    return pb.GatewayMessage(session_open=pb.SessionOpenRequest(
        protocol_version="1.0", session_id="s1", tenant_id="t1",
        direction="test", test_credential=credential,
    ))


async def _converse(factory, caplog) -> list[pb.ServiceMessage]:
    servicer = ConversationServicer(factory)
    with caplog.at_level(logging.DEBUG):
        return [m async for m in servicer.Converse(_stream(_open()), AsyncMock())]


async def test_agent_unavailable_yields_one_fatal_error_and_ends(caplog):
    async def refuse(ctx):
        raise AgentUnavailable()

    out = await _converse(refuse, caplog)

    assert len(out) == 1
    err = out[0].error
    assert out[0].WhichOneof("payload") == "error"
    assert (err.code, err.message, err.fatal) == ("AGENT_UNAVAILABLE", "agent unavailable", True)
    assert err.session_id == "s1"


async def test_open_log_never_contains_credential(caplog):
    seen = []

    async def refuse(ctx):
        seen.append(ctx.test_credential)
        raise AgentUnavailable()

    await _converse(refuse, caplog)

    assert seen == [SENTINEL]  # the credential did reach the factory
    assert caplog.records
    assert SENTINEL not in caplog.text


async def test_non_test_session_still_opens(caplog):
    handler = AsyncMock()
    handler.greeting.return_value = []

    async def factory(ctx):
        return handler

    responses = ConversationServicer(factory).Converse(_stream(_open("")), AsyncMock())
    first = await anext(responses)
    await responses.aclose()

    assert first.WhichOneof("payload") == "service_ready"


DRIVER_ERROR = "connect to 10.0.0.5:5432 postgresql://u:p@h/db"


def _audio() -> pb.GatewayMessage:
    return pb.GatewayMessage(audio_chunk=pb.AudioChunk(payload=b"\x00\x00", trace_id="tr1"))


async def _converse_with(factory, caplog, *messages) -> list[pb.ServiceMessage]:
    servicer = ConversationServicer(factory)
    with caplog.at_level(logging.DEBUG):
        return [m async for m in servicer.Converse(_stream(*messages), AsyncMock())]


def _code_message_fatal(out: list[pb.ServiceMessage]) -> tuple[str, str, bool]:
    err = out[-1].error
    assert out[-1].WhichOneof("payload") == "error"
    return (err.code, err.message, err.fatal)


async def test_factory_raise_yields_generic_internal_error(caplog):
    async def broken(ctx):
        raise OSError(DRIVER_ERROR)

    out = await _converse(broken, caplog)

    assert len(out) == 1
    assert _code_message_fatal(out) == ("INTERNAL", "internal error", True)
    assert out[0].error.session_id == "s1"
    assert DRIVER_ERROR in caplog.text  # full detail stays in the server log


async def test_resolver_db_failure_still_greets(caplog, monkeypatch):
    @asynccontextmanager
    async def down(pool, *, explicit_tenant, reason):
        raise OSError(DRIVER_ERROR)
        yield  # pragma: no cover

    monkeypatch.setattr(policy_resolver, "tenant_conn", down)
    resolver = ToolPolicyResolver(pool=object(), registry=ToolRegistry())
    handler = AsyncMock()
    handler.greeting.return_value = [b"hello"]

    async def factory(ctx):
        assert await resolver.enabled_tools("agent1", ctx.tenant_id) == []
        return handler

    responses = ConversationServicer(factory).Converse(_stream(_open("")), AsyncMock())
    kinds = [(await anext(responses)).WhichOneof("payload") for _ in range(2)]
    await responses.aclose()

    assert kinds == ["service_ready", "tts_started"]


async def test_mid_call_handler_raise_yields_generic_internal_error(caplog):
    handler = AsyncMock()
    handler.greeting.return_value = []
    handler.out_responses = None  # AsyncMock would invent a queue
    handler.on_audio.side_effect = OSError(DRIVER_ERROR)

    async def factory(ctx):
        return handler

    out = await _converse_with(factory, caplog, _open(""), _audio())

    assert out[0].WhichOneof("payload") == "service_ready"
    assert _code_message_fatal(out) == ("INTERNAL", "internal error", True)
    assert DRIVER_ERROR in caplog.text


async def test_rejected_call_hangup_yields_generic_message(caplog):
    handler = AsyncMock()
    handler.greeting.return_value = []
    handler.out_responses = None  # AsyncMock would invent a queue
    handler.on_audio.side_effect = RejectedCallHangup("caller kept talking session=abc")

    async def factory(ctx):
        return handler

    out = await _converse_with(factory, caplog, _open(""), _audio())

    assert _code_message_fatal(out) == ("CALL_REJECTED", "call rejected", True)
    assert "session=abc" in caplog.text
